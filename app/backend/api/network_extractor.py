"""
NetGravity — Dynamic Excel & CSV Structural Extractor
=====================================================
Parses uploaded user workbooks (.xlsx, .xls, .csv), detects sheet/table schemas,
and extracts Plants, DCs, Markets and Lanes for *mapping review*.

It does NOT optimise. The duplicate MILP that used to live at the bottom of this
module was removed in Phase 10.0 — see the banner at the end of the file.
Optimisation belongs to `netgravity/optimization/milp.py`, reached through the
orchestrator.
"""

from __future__ import annotations

import io
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

# Geocoordinates catalog for Indian and global cities
CITY_COORDINATES: Dict[str, Tuple[float, float, str]] = {
    "delhi": (28.61, 77.21, "North"),
    "delhi ncr": (28.61, 77.21, "North"),
    "ncr": (28.61, 77.21, "North"),
    "baddi": (30.96, 76.79, "North"),
    "mumbai": (19.08, 72.88, "West"),
    "pune": (18.52, 73.86, "West"),
    "bengaluru": (12.97, 77.59, "South"),
    "bangalore": (12.97, 77.59, "South"),
    "chennai": (13.08, 80.27, "South"),
    "hyderabad": (17.38, 78.49, "South"),
    "kolkata": (22.57, 88.36, "East"),
    "ahmedabad": (23.03, 72.57, "West"),
    "jaipur": (26.91, 75.79, "North"),
    "lucknow": (26.85, 80.95, "North"),
    "guwahati": (26.14, 91.74, "Northeast"),
    "patna": (25.59, 85.13, "East"),
    "chandigarh": (30.73, 76.78, "North"),
    "indore": (22.71, 75.85, "Central"),
    "nagpur": (21.14, 79.08, "Central"),
    "coimbatore": (11.01, 76.96, "South"),
    "kochi": (9.93, 76.26, "South"),
    "surat": (21.17, 72.83, "West"),
    "bhopal": (23.25, 77.41, "Central"),
    "visakhapatnam": (17.68, 83.21, "South"),
}


def lookup_coordinates(city_name: str, index: int = 0) -> Tuple[float, float, str]:
    if not city_name:
        # Default circular distribution across India if unknown
        angle = (2 * math.pi * (index % 12)) / 12
        return (21.0 + 6.0 * math.sin(angle), 78.0 + 7.0 * math.cos(angle), "Central")

    clean = str(city_name).strip().lower()
    for key, (lat, lng, reg) in CITY_COORDINATES.items():
        if key in clean or clean in key:
            return (lat, lng, reg)

    # Fallback to deterministic grid coordinate
    hash_val = sum(ord(c) for c in clean)
    lat = 12.0 + (hash_val % 160) / 10.0
    lng = 72.0 + ((hash_val * 7) % 180) / 10.0
    return (round(lat, 2), round(lng, 2), "National")


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------
# Every one of these returns None rather than a substitute. The previous
# extractor defaulted capacity to 10,000 units, freight to ₹10/unit, distance
# to 300km, lead time to 1 day and handling to ₹4/unit whenever it failed to
# match a column — so a workbook whose headers it did not recognise was solved
# against uniform invented economics and reported as the user's own network.

def _num(row, col) -> Optional[float]:
    """A numeric cell, or None. Never a default."""
    if not col:
        return None
    try:
        value = row.get(col)
    except Exception:  # noqa: BLE001 - row may be a plain Series
        return None
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _text(row, col) -> str:
    if not col:
        return ""
    try:
        value = row.get(col)
    except Exception:  # noqa: BLE001
        return ""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _pick(cols_lower: Dict[str, Any], *names: str) -> Optional[Any]:
    """First matching column, by exact lowercase header name."""
    for n in names:
        if n in cols_lower:
            return cols_lower[n]
    return None


def _has(cols_lower: Dict[str, Any], *names: str) -> bool:
    return any(n in cols_lower for n in names)


#: Column aliases, kept in one place so a new client dialect is a one-line
#: change rather than an edit scattered through the parsing branches.
FACILITY_ID_COLS = ("facility_id", "plant_id", "dc_id", "site_id", "node_id", "warehouse_id")
MARKET_ID_COLS = ("market_id", "customer_id", "destination_market", "market")
PRODUCT_ID_COLS = ("product_id", "sku_id", "sku", "item_id")
PERIOD_COLS = ("period", "month", "date", "year_month", "week")
LANE_ID_COLS = ("lane_id", "route_id")
CAPACITY_COLS = ("capacity_units", "capacity_units_per_day", "capacity_units_per_period",
                 "capacity", "cap", "max_units", "available_capacity_units")
RATE_COLS = ("rate_per_unit", "transport_cost_per_unit", "cost_per_unit", "freight_rate",
             "rate", "cost", "freight", "transport_cost")
LEAD_COLS = ("transit_time_days", "lead_time_days", "transit_days", "lead_time", "transit_time")
DEMAND_COLS = ("demand_units", "demand_quantity", "quantity_units", "demand",
               "quantity", "units", "volume")
DISTANCE_KM_COLS = ("distance_km", "distance_kms", "distance_kilometres",
                    "distance_kilometers", "distance", "dist", "km")
#: Distance stated in miles. A US workbook says `Distance_Miles`, which matched
#: no alias at all, so every lane was read with no distance: 51 corridors at
#: 0 km, zero carbon, and a distance-weighted average of zero. Miles are a unit
#: of the same quantity, not a different field, so they are converted here and
#: the conversion is stated in the notes rather than performed silently.
DISTANCE_MI_COLS = ("distance_miles", "distance_mi", "distance_mile", "miles", "mi")
#: One statute mile in kilometres, exactly.
MILES_TO_KM = 1.609344
MODE_COLS = ("transport_mode", "mode", "transportation_mode", "shipping_mode",
             "service_mode", "freight_mode", "modal")
HANDLING_COLS = ("handling_cost_per_unit", "handling_cost", "variable_cost_per_unit",
                 "cost_per_unit_handled", "handling_rate")
COST_TYPE_COLS = ("cost_type", "cost_category", "expense_type")
EFFECTIVE_DATE_COLS = ("effective_date", "valid_from", "effective_from", "as_of")
CURRENCY_COLS = ("currency", "ccy", "rate_currency", "currency_code")

#: ISO 4217 codes this build recognises when a header names its unit, e.g.
#: `Monthly_Cost_USD`. Deliberately a closed list: a three-letter suffix that
#: is not a currency (`Facility_ID_XYZ`) must not be read as one.
_ISO_CURRENCIES: frozenset = frozenset({
    "INR", "USD", "EUR", "GBP", "JPY", "CNY", "AUD", "CAD", "CHF", "SGD",
    "AED", "SAR", "ZAR", "BRL", "MXN", "SEK", "NOK", "DKK", "PLN", "TRY",
    "THB", "IDR", "MYR", "PHP", "VND", "KRW", "HKD", "NZD", "ILS", "RUB",
})

#: Transport modes the telemetry layer can describe. An uploaded value is
#: normalised onto one of these when it is recognised and kept verbatim
#: otherwise — a mode this build has no opinion about is still the client's
#: mode, and overwriting it with "ROAD" is what produced a US network whose
#: rail and intermodal corridors all reported as road freight.
_MODE_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "ROAD": ("road", "truck", "trucking", "tl", "truckload", "ftl", "ltl",
             "less_than_truckload", "less than truckload", "surface", "highway"),
    "RAIL": ("rail", "train", "railway", "railroad"),
    "INTERMODAL": ("intermodal", "multimodal", "imdl", "rail_road", "piggyback"),
    "AIR": ("air", "airfreight", "air_freight", "aviation", "flight"),
    "SEA": ("sea", "ocean", "vessel", "marine", "shipping", "fcl", "lcl", "barge"),
    "PARCEL": ("parcel", "courier", "express", "small_parcel"),
}


def normalise_transport_mode(raw: str) -> Optional[str]:
    """
    Map an uploaded transport-mode value onto a known mode.

    Returns the upper-cased original when the value is unrecognised, and None
    when the cell is empty. It never returns a substitute for a missing value:
    a lane with no stated mode has no mode, which is a different fact from a
    lane that travels by road.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    key = text.lower().replace("-", "_").replace(" ", "_")
    for mode, aliases in _MODE_SYNONYMS.items():
        if key in aliases or key.replace("_", " ") in aliases:
            return mode
    return text.upper()


#: Coarse geographic extent, from coordinates the upload actually states.
#:
#: Every project was created as `region="India"` regardless of its data, so a
#: US network was labelled an India network on every screen that names the
#: geography. The bounding box of the uploaded nodes is evidence the file
#: carries; this reads it rather than assuming. Anything that does not sit
#: inside one of these boxes is reported as "Global", never as a guess.
_REGION_BOXES: Tuple[Tuple[str, float, float, float, float], ...] = (
    # (label, lat_min, lat_max, lng_min, lng_max)
    ("India", 6.0, 37.5, 67.0, 98.0),
    ("United States", 18.0, 72.0, -180.0, -66.0),
    ("Europe", 34.0, 72.0, -12.0, 45.0),
    ("Southeast Asia", -11.0, 29.0, 92.0, 142.0),
    ("China", 17.0, 54.0, 73.0, 135.0),
    ("Middle East", 12.0, 42.0, 34.0, 64.0),
    ("Africa", -35.0, 38.0, -18.0, 52.0),
    ("South America", -56.0, 13.0, -82.0, -34.0),
    ("Oceania", -48.0, -9.0, 110.0, 180.0),
)


def infer_geography(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Where this network is, from the coordinates in the upload.

    Returns the inferred region label, the bounding box a map should fit to,
    and the share of nodes that fall inside the chosen box — so a caller can
    show the basis for the label instead of asserting it. `region` is None when
    the upload carries no usable coordinates at all.
    """
    points = [(n.get("latSource", n.get("lat")), n.get("lngSource", n.get("lng")))
              for n in nodes]
    points = [(la, ln) for la, ln in points
              if isinstance(la, (int, float)) and isinstance(ln, (int, float))]
    if not points:
        return {"region": None, "bounds": None, "confidence": 0.0,
                "basis": "The upload carries no latitude/longitude values."}

    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    bounds = {"latMin": min(lats), "latMax": max(lats),
              "lngMin": min(lngs), "lngMax": max(lngs)}

    best_label, best_share = None, 0.0
    for label, la0, la1, ln0, ln1 in _REGION_BOXES:
        inside = sum(1 for la, ln in points if la0 <= la <= la1 and ln0 <= ln <= ln1)
        share = inside / len(points)
        if share > best_share:
            best_label, best_share = label, share

    # A network spanning two continents is not "mostly" either of them.
    if best_share < 0.6:
        return {"region": "Global", "bounds": bounds, "confidence": round(best_share, 3),
                "basis": (f"{len(points)} node coordinate(s) do not fall predominantly "
                          f"inside any single region.")}
    return {
        "region": best_label,
        "bounds": bounds,
        "confidence": round(best_share, 3),
        "basis": (f"{int(best_share * len(points))} of {len(points)} node "
                  f"coordinate(s) fall inside {best_label}."),
    }


def classify_sheet(df: "pd.DataFrame") -> str:
    """
    Decide what one sheet *is*, from its column signature.

    This replaces the previous approach of testing every sheet against every
    branch, which let one sheet be read as several things at once: the
    `Capacity` sheet (facility_id + period) matched the facilities branch and
    silently re-registered all eight facilities a second time, adding the three
    plants to the DC list as well — the network showed 3 plants and 8 DCs for a
    workbook containing 3 plants and 5 DCs.

    Order matters: the time-series sheets are checked before the master sheets
    they share an id column with.
    """
    cols = {str(c).strip().lower() for c in df.columns}
    has = lambda *n: any(x in cols for x in n)  # noqa: E731

    # --- time series first (they share id columns with the master tables) ---
    if has(*PERIOD_COLS):
        if has(*MARKET_ID_COLS) and has(*DEMAND_COLS):
            return "demand_history"
        if has(*FACILITY_ID_COLS):
            return "capacity_history"

    # --- reference tables keyed by another table's id ---
    if has(*LANE_ID_COLS) and has(*RATE_COLS) and not has("origin_id", "from", "origin"):
        return "lane_rates"
    # A warehouse cost table is keyed by facility and states a cost line per
    # site, not a site. Checked before "facilities" for the same reason the
    # time series are: it shares the facility id column with the master sheet.
    #
    # Until this existed the sheet fell to "unknown" and nothing was read from
    # it, so `Cost_Per_Unit_Handled` — which every DC in the US workbook states
    # — never reached the model. Handling cost was zero network-wide and the
    # optimiser moved units between sites as if handling them were free.
    if has(*FACILITY_ID_COLS) and (has(*COST_TYPE_COLS) or has("cost_per_unit_handled")):
        return "warehouse_costs"

    # --- master tables ---
    if has("origin_id", "destination_id") or (has("from") and has("to")):
        return "lanes"
    if has(*PRODUCT_ID_COLS) and has("product_name", "name") and not has(*MARKET_ID_COLS):
        return "products"
    if has("signal_id") or (has("signal_type") and has(*MARKET_ID_COLS)):
        return "signals"
    if has(*MARKET_ID_COLS):
        return "markets"
    if has(*FACILITY_ID_COLS) or has("facility_name", "facility_type"):
        return "facilities"

    return "unknown"


#: The canonical fields this application actually reads, per sheet role, each
#: bound to the SAME alias tuples the parsing branches use above.
#:
#: Why this is derived rather than listed independently: the mapping-review
#: screen exists so a user can confirm that what we read is what they meant. A
#: second, hand-maintained vocabulary answered that question about a schema the
#: extractor does not use — on the sample workbook it reported 42 of 51 columns
#: as "No match found", including Latitude, Capacity_Units, Fixed_Cost,
#: Rate_Per_Unit and Demand_Units, every one of which the extractor reads
#: correctly. The screen was describing a different program.
#:
#: Each entry is (canonical label, alias names). A column matches when its
#: lower-cased name is in the alias tuple, so `Rate_Per_Unit` matches
#: "rate_per_unit" exactly instead of relying on a word-boundary regex, which
#: never fired on snake_case names at all (`cost` cannot match inside
#: "unit_cost", because "_" is a word character).
_COLUMN_ROLES: Dict[str, Tuple[Tuple[str, Tuple[str, ...]], ...]] = {
    "facilities": (
        ("Facility ID", FACILITY_ID_COLS),
        ("Facility name", ("facility_name", "name", "dc_name", "plant_name", "site_name")),
        ("Facility type", ("facility_type", "type", "role", "node_type")),
        ("Facility capacity", CAPACITY_COLS),
        ("Fixed cost", ("fixed_cost", "fixed_cost_monthly", "fixed_cost_per_month",
                        "annual_fixed_cost", "fixed_cost_per_year")),
        ("Handling cost per unit", HANDLING_COLS),
        ("Latitude", ("latitude", "lat")),
        ("Longitude", ("longitude", "lng", "lon", "long")),
        ("City", ("city", "location", "town")),
        ("State", ("state", "province")),
        ("Status", ("status", "active")),
        # What it costs, once, to open a site that is not open yet.
        #
        # The MILP has always priced this — `opening_cost_term` in milp.py
        # charges it on any CANDIDATE the solver switches on — but no column
        # fed it, so every proposed site was offered to the optimiser as free
        # to build. A candidate that costs nothing to open and saves anything
        # at all is always worth opening, which is why "should we open a new
        # DC?" could only ever come back yes.
        ("Opening cost", ("opening_cost", "cost_to_open", "one_time_opening_cost",
                          "setup_cost", "capex", "capital_cost")),
    ),
    "markets": (
        ("Market ID", MARKET_ID_COLS),
        ("Market name", ("market_name", "name", "customer_name")),
        ("Service SLA (days)", ("service_sla_days", "sla_days", "sla", "service_level_days")),
        ("Demand quantity", DEMAND_COLS),
        ("Latitude", ("latitude", "lat")),
        ("Longitude", ("longitude", "lng", "lon", "long")),
        ("City", ("city", "location", "town")),
        ("State", ("state", "province")),
        ("Region", ("region", "zone", "territory")),
    ),
    "lanes": (
        ("Lane ID", LANE_ID_COLS),
        ("Origin", ("origin_id", "from", "origin", "source_id")),
        ("Destination", ("destination_id", "to", "destination", "dest_id")),
        ("Distance (km)", DISTANCE_KM_COLS),
        ("Distance (miles, converted to km)", DISTANCE_MI_COLS),
        ("Transport mode", MODE_COLS),
        ("Transit time (days)", LEAD_COLS),
        ("Lane capacity", ("capacity_units", "lane_capacity", "max_units")),
        ("Freight rate", RATE_COLS),
        ("Lane active", ("active", "is_active", "status")),
        ("Origin type", ("origin_type", "from_type")),
        ("Destination type", ("destination_type", "dest_type", "to_type")),
    ),
    "products": (
        ("Product ID", PRODUCT_ID_COLS),
        ("Product name", ("product_name", "name")),
        ("Unit weight (kg)", ("unit_weight_kg", "weight_kg", "weight")),
        ("Unit value", ("unit_cost", "cost_per_unit", "value", "unit_value")),
    ),
    "demand_history": (
        ("Period", PERIOD_COLS),
        ("Market ID", MARKET_ID_COLS),
        ("Product ID", PRODUCT_ID_COLS),
        ("Demand quantity", DEMAND_COLS),
    ),
    "capacity_history": (
        ("Facility ID", FACILITY_ID_COLS),
        ("Period", PERIOD_COLS),
        ("Available capacity", ("available_capacity_units", "available_capacity",
                                "capacity_units")),
        ("Used capacity", ("used_capacity_units", "used_capacity", "utilised_capacity")),
    ),
    "lane_rates": (
        ("Lane ID", LANE_ID_COLS),
        ("Product ID", PRODUCT_ID_COLS),
        ("Freight rate", RATE_COLS),
        ("Currency", CURRENCY_COLS),
    ),
    "warehouse_costs": (
        ("Facility ID", FACILITY_ID_COLS),
        ("Cost type", COST_TYPE_COLS),
        ("Handling cost per unit", HANDLING_COLS),
        ("Monthly facility cost", ("monthly_cost", "monthly_cost_usd", "monthly_cost_inr",
                                   "cost_per_month", "monthly_amount")),
        ("Effective date", EFFECTIVE_DATE_COLS),
    ),
    "signals": (
        ("Signal ID", ("signal_id", "id")),
        ("Signal date", ("signal_date", "date", "published_date")),
        ("Signal type", ("signal_type", "type", "category")),
        ("Market ID", MARKET_ID_COLS),
        ("Description", ("description", "headline", "summary", "detail")),
        ("Relevance", ("relevance", "confidence", "impact")),
        ("Event probability", ("event_probability", "probability", "likelihood")),
    ),
}

#: The sheet name this application looks for, per role. Any of the aliases in
#: `classify_sheet` still work — a sheet is identified by its columns, not its
#: name — but these are the names the downloadable template uses, so a workbook
#: built from it classifies without ambiguity.
SHEET_TEMPLATE_NAMES: Dict[str, str] = {
    "facilities": "Facilities",
    "markets": "Markets",
    "lanes": "Lanes",
    "products": "Products",
    "demand_history": "Demand_History",
    "capacity_history": "Capacity_History",
    "lane_rates": "Lane_Rates",
    "warehouse_costs": "Warehouse_Costs",
    "signals": "Signals",
}


def upload_schema() -> List[Dict[str, Any]]:
    """
    The sheets and columns this build reads, for the upload template.

    Derived from `_COLUMN_ROLES` — the same table `classify_column_name` uses —
    so a template can never describe a column the parser does not recognise, and
    a column added to the parser appears in the template without anyone
    remembering to add it. The first alias is the preferred header because it is
    the one `classify_sheet` and `classify_column_name` were written around.
    """
    sheets: List[Dict[str, Any]] = []
    for role, entries in _COLUMN_ROLES.items():
        columns = []
        for label, aliases in entries:
            ordered = list(dict.fromkeys(aliases))
            columns.append({
                "label": label,
                "header": ordered[0] if ordered else label,
                "accepted": ordered,
            })
        sheets.append({
            "role": role,
            "sheet": SHEET_TEMPLATE_NAMES.get(role, role.title()),
            "columns": columns,
        })
    return sheets


#: What the UI offers in its "mapped to" dropdown, and the label for a column
#: this build does not read.
NOT_USED = "Not used by the model"

CANONICAL_FIELDS: Tuple[str, ...] = tuple(
    dict.fromkeys(
        [label for entries in _COLUMN_ROLES.values() for label, _ in entries]
    )
) + (NOT_USED,)


def classify_column_name(
    col: str, sheet_role: Optional[str] = None,
) -> Tuple[str, str, float]:
    """
    Say what this application will do with one uploaded column.

    Returns `(canonical_field, status, confidence)`, where status is:
      * "auto"    — recognised, and this build reads it;
      * "review"  — the sheet's role could not be determined, so the match is a
                    guess across every role rather than a decision;
      * "ignored" — parsed but not consumed by any engine here.

    `sheet_role` comes from `classify_sheet()`, so the answer is the one the
    extractor will actually act on: `Capacity_Units` is the facility's capacity
    on a facilities sheet and the lane's capacity on a lanes sheet, and saying
    so is the whole point of a mapping-review screen.
    """
    name = str(col).strip().lower()

    entries = _COLUMN_ROLES.get(sheet_role or "")
    if entries:
        for label, aliases in entries:
            if name in aliases:
                return (label, "auto", 0.97)
        return (NOT_USED, "ignored", 0.40)

    # No sheet role: fall back to matching across every role. A name that means
    # one thing everywhere is still certain; one that differs by sheet is
    # flagged for review rather than silently resolved to whichever role
    # happened to be checked first.
    matches = {
        label
        for role_entries in _COLUMN_ROLES.values()
        for label, aliases in role_entries
        if name in aliases
    }
    if len(matches) == 1:
        return (next(iter(matches)), "auto", 0.90)
    if matches:
        return (sorted(matches)[0], "review", 0.60)
    return (NOT_USED, "ignored", 0.40)


def extract_tables_from_file(file_storage) -> Dict[str, pd.DataFrame]:
    """
    Reads an uploaded FileStorage object (Excel or CSV) into a dict of DataFrames.
    """
    filename = getattr(file_storage, "filename", "upload.csv").lower()
    stream = file_storage.stream if hasattr(file_storage, "stream") else file_storage

    dfs: Dict[str, pd.DataFrame] = {}

    # Parse failures are raised, not swallowed. The previous version wrapped
    # both branches in `except Exception: pass`, which hid a real defect: the
    # CSV branch referenced `Path` without importing it, so EVERY csv upload
    # raised NameError, was silently caught, and returned zero tables — the
    # user saw "0 columns detected" with no error anywhere.
    if filename.endswith((".xlsx", ".xls", ".xlsm")):
        engine = "openpyxl" if filename.endswith((".xlsx", ".xlsm")) else None
        excel_file = pd.ExcelFile(stream, engine=engine)
        sheet_errors: List[str] = []
        for sheet in excel_file.sheet_names:
            try:
                df = excel_file.parse(sheet)
            except Exception as exc:  # noqa: BLE001 — recorded per sheet
                sheet_errors.append(f"{sheet}: {exc}")
                continue
            if not df.empty and len(df.columns) > 1:
                dfs[sheet] = df.dropna(how="all")
        if not dfs and sheet_errors:
            raise ValueError(
                f"No readable sheet in '{filename}'. " + "; ".join(sheet_errors[:3])
            )
    else:
        content = stream.read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        sep = "\t" if filename.endswith(".tsv") else ","
        df = pd.read_csv(io.BytesIO(content), sep=sep)
        dfs[Path(filename).stem] = df.dropna(how="all")

    return dfs


def _coords(row, cols_lower, fallback_label: str, index: int,
            notes: List[str], entity: str) -> Tuple[float, float, str, bool]:
    """
    Coordinates for one row.

    An explicit Latitude/Longitude pair in the upload always wins. The city
    lookup is a fallback, and a hash-grid position is a last resort that is
    recorded as a note — it used to be applied silently even when the sheet
    carried real coordinates, which put every market at a fictional point.
    """
    lat = _num(row, _pick(cols_lower, "latitude", "lat"))
    lng = _num(row, _pick(cols_lower, "longitude", "lng", "lon", "long"))
    region = _text(row, _pick(cols_lower, "region", "zone"))

    if lat is not None and lng is not None:
        if not region:
            _, _, region = lookup_coordinates(fallback_label, index)
        return lat, lng, region or "National", True

    look_lat, look_lng, look_reg = lookup_coordinates(fallback_label, index)
    if fallback_label and str(fallback_label).strip().lower() in CITY_COORDINATES:
        notes.append(
            f"{entity}: no latitude/longitude column; position taken from the "
            f"city name '{fallback_label}'."
        )
    else:
        notes.append(
            f"{entity}: no latitude/longitude and '{fallback_label}' is not a "
            f"known city — placed at an approximate position for display only."
        )
    return look_lat, look_lng, region or look_reg, False


def build_network_from_dataframes(tables: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """
    Build the structural view of a user's upload.

    Each sheet is classified once (see `classify_sheet`) and read for what it
    is, which is what makes a *normalised* workbook work: a real client sends
    demand as a `Demand_History` time series and freight as a
    `Transportation_Rates` table keyed by lane and product, not as extra
    columns on the markets and lanes sheets. The previous version could only
    read the denormalised shape, and filled the gaps with invented numbers.

    Returns the four node/edge collections plus `products`, `demandHistory`,
    `capacityHistory`, `signals` and `notes` — every assumption in words.
    """
    plants: List[Dict[str, Any]] = []
    dcs: List[Dict[str, Any]] = []
    markets: List[Dict[str, Any]] = []
    lanes: List[Dict[str, Any]] = []
    products: List[Dict[str, Any]] = []
    demand_history: List[Dict[str, Any]] = []
    capacity_history: List[Dict[str, Any]] = []
    signals: List[Dict[str, Any]] = []
    notes: List[str] = []

    seen_plants: set = set()
    seen_dcs: set = set()
    seen_markets: set = set()

    # Classify every sheet up front so each is read exactly once, in an order
    # that lets later passes join onto earlier ones.
    by_role: Dict[str, List[Tuple[str, pd.DataFrame]]] = {}
    for sheet_name, df in tables.items():
        by_role.setdefault(classify_sheet(df), []).append((sheet_name, df))

    def sheets(role: str):
        return by_role.get(role, [])

    # ---- Products ----------------------------------------------------
    for _, df in sheets("products"):
        cl = {str(c).strip().lower(): c for c in df.columns}
        id_col = _pick(cl, *PRODUCT_ID_COLS)
        for _, row in df.iterrows():
            pid = _text(row, id_col)
            if not pid:
                continue
            products.append({
                "id": pid,
                "name": _text(row, _pick(cl, "product_name", "name")) or pid,
                "category": _text(row, _pick(cl, "product_category", "category")) or None,
                "unitWeightKg": _num(row, _pick(cl, "unit_weight_kg", "weight_kg", "weight")),
                "unitCost": _num(row, _pick(cl, "unit_cost", "cost_per_unit", "value")),
            })

    # ---- Facilities --------------------------------------------------
    for _, df in sheets("facilities"):
        cl = {str(c).strip().lower(): c for c in df.columns}
        id_col = _pick(cl, *FACILITY_ID_COLS) or df.columns[0]
        name_col = _pick(cl, "facility_name", "name", "dc_name", "plant_name")
        type_col = _pick(cl, "facility_type", "type", "role", "node_type")
        city_col = _pick(cl, "city", "location", "town")
        state_col = _pick(cl, "state", "province")
        cap_col = _pick(cl, *CAPACITY_COLS)
        fixed_col = _pick(cl, "fixed_cost", "fixed_cost_monthly", "fixed_cost_per_month",
                          "annual_fixed_cost", "fixed_cost_per_year")
        handling_col = _pick(cl, *HANDLING_COLS)
        status_col = _pick(cl, "status", "active")
        opening_col = _pick(cl, "opening_cost", "cost_to_open", "one_time_opening_cost",
                            "setup_cost", "capex", "capital_cost")

        for idx, row in df.iterrows():
            fac_id = _text(row, id_col)
            if not fac_id or fac_id in seen_plants or fac_id in seen_dcs:
                continue
            fac_name = _text(row, name_col) or fac_id
            raw_type = _text(row, type_col).upper()
            city = _text(row, city_col)
            lat, lng, region, exact = _coords(
                row, cl, city or fac_name, idx, notes, f"Facility {fac_id}")

            capacity = _num(row, cap_col)
            if capacity is None:
                notes.append(
                    f"Facility {fac_id}: no capacity column was recognised; the "
                    f"facility is treated as uncapacitated rather than given a "
                    f"default size."
                )

            stated_status = _text(row, status_col)
            status = (stated_status or "ACTIVE").upper()
            is_plant = ("PLANT" in raw_type or "MANUFACTUR" in raw_type
                        or fac_id.upper().startswith("PLT")
                        or (not raw_type and "PLANT" in fac_name.upper()))

            common = {
                "id": fac_id,
                "name": fac_name,
                "city": city or fac_name,
                # `state` is the state, not the region. The two were conflated,
                # so Mumbai rendered as being in "West".
                "state": _text(row, state_col) or None,
                "lat": lat,
                "lng": lng,
                "capacity": capacity,
                "region": region,
                "status": "EXISTING" if status in ("ACTIVE", "TRUE", "EXISTING", "1") else status,
                # Whether the UPLOAD said that, or whether the line above
                # supplied it. `status` is defaulted to EXISTING so a file with
                # no status column is not handed to the solver as a network of
                # greenfield sites it may close — but "the client said this is
                # operating" and "we assumed it was" are different facts, and a
                # completeness check that cannot tell them apart either invents
                # a gap or hides one. Read by
                # netgravity/ingestion/completeness_sources.py.
                "statusStated": bool(stated_status),
                "coordsExact": exact,
                # Read for EVERY facility, not only DCs. A plant carries a
                # fixed cost in exactly the same column, and reading it only
                # for DCs silently dropped it: the sample workbook states
                # ₹3,852,000/month for its Mumbai plant alone, and all three
                # plants together came to more than twice the fixed cost the
                # network reported. Both stay None when the column is absent.
                "fixedCost": _num(row, fixed_col),
                "handlingCost": _num(row, handling_col),
                # One-time cost to open, for a site that is not open yet. None
                # when the sheet states none — NOT zero, which the solver would
                # read as "free to build" and act on.
                "openingCost": _num(row, opening_col),
            }
            # No "throughput"/"utilPct": a facility's flow is produced by the
            # solver, not read from a facility sheet.
            if is_plant:
                seen_plants.add(fac_id)
                plants.append(common)
            else:
                seen_dcs.add(fac_id)
                dcs.append(common)

    # ---- Markets -----------------------------------------------------
    for _, df in sheets("markets"):
        cl = {str(c).strip().lower(): c for c in df.columns}
        id_col = _pick(cl, *MARKET_ID_COLS)
        name_col = _pick(cl, "market_name", "name", "customer_name")
        city_col = _pick(cl, "city", "location", "town")
        sla_col = _pick(cl, "service_sla_days", "sla_days", "sla", "service_level_days")
        demand_col = _pick(cl, *DEMAND_COLS)

        for idx, row in df.iterrows():
            m_id = _text(row, id_col)
            if not m_id or m_id in seen_markets:
                continue
            seen_markets.add(m_id)
            city = _text(row, city_col)
            name = _text(row, name_col) or city or m_id
            lat, lng, region, exact = _coords(
                row, cl, city or name, idx, notes, f"Market {m_id}")
            markets.append({
                "id": m_id,
                "name": name,
                "city": city or None,
                "state": _text(row, _pick(cl, "state", "province")) or None,
                "lat": lat,
                "lng": lng,
                # A markets master sheet often carries no demand at all — the
                # demand lives in a history table. Left as None here and filled
                # from that history below, never defaulted.
                "demand": _num(row, demand_col),
                "slaDays": _num(row, sla_col),
                "priority": None,
                "region": region,
                "coordsExact": exact,
            })

    # ---- Lanes -------------------------------------------------------
    miles_converted = 0
    modes_read: Dict[str, int] = {}
    #: Every lane id the sheet DEFINES, including the inactive ones the model
    #: does not carry. A rate priced against a lane the client has switched off
    #: is unused, not dangling, and reporting it as a broken reference would
    #: send someone hunting for a row that is exactly where it should be.
    defined_lane_ids: set = set()
    for _, df in sheets("lanes"):
        cl = {str(c).strip().lower(): c for c in df.columns}
        lane_id_col = _pick(cl, *LANE_ID_COLS)
        from_col = _pick(cl, "origin_id", "from", "origin", "source_id")
        to_col = _pick(cl, "destination_id", "to", "destination", "dest_id")
        dist_col = _pick(cl, *DISTANCE_KM_COLS)
        # Only consulted when the sheet states no kilometre column, so a
        # workbook carrying both is read in its canonical unit.
        miles_col = _pick(cl, *DISTANCE_MI_COLS)
        mode_col = _pick(cl, *MODE_COLS)
        rate_col = _pick(cl, *RATE_COLS)
        lead_col = _pick(cl, *LEAD_COLS)
        dest_type_col = _pick(cl, "destination_type", "dest_type", "to_type")
        active_col = _pick(cl, "active", "is_active", "status")
        cap_col = _pick(cl, "capacity_units", "lane_capacity", "max_units")

        for _, row in df.iterrows():
            f_id, t_id = _text(row, from_col), _text(row, to_col)
            if not f_id or not t_id:
                continue
            lane_id = _text(row, lane_id_col)
            if lane_id:
                defined_lane_ids.add(lane_id)
            active = _text(row, active_col).upper()
            if active in ("FALSE", "0", "N", "NO", "INACTIVE"):
                continue

            distance_km = _num(row, dist_col)
            distance_source = "km" if distance_km is not None else None
            if distance_km is None and miles_col is not None:
                miles = _num(row, miles_col)
                if miles is not None:
                    distance_km = round(miles * MILES_TO_KM, 3)
                    distance_source = "miles"
                    miles_converted += 1

            mode = normalise_transport_mode(_text(row, mode_col))
            if mode:
                modes_read[mode] = modes_read.get(mode, 0) + 1

            lanes.append({
                "laneId": _text(row, lane_id_col) or None,
                "from": f_id,
                "to": t_id,
                "distance": distance_km,
                #: Which unit the distance was stated in, so a screen can say
                #: "converted from miles" rather than presenting a derived
                #: number as though it were the source's own.
                "distanceSource": distance_source,
                # None when the sheet carries no rate. Rates are joined from a
                # Transportation_Rates table below when one exists.
                "cost": _num(row, rate_col),
                "leadTime": _num(row, lead_col),
                "capacity": _num(row, cap_col),
                # Flow is a solver OUTPUT, not an input.
                "flow": None,
                # The uploaded mode, or None. This was the literal "ROAD" for
                # every lane ever parsed, which reported a rail-and-intermodal
                # US network as pure road freight — and made mode-sensitive
                # cost, emissions and modal-shift analysis meaningless.
                "mode": mode,
                "destType": _text(row, dest_type_col).upper() or None,
            })

    if miles_converted:
        notes.append(
            f"{miles_converted} lane(s) state distance in miles; converted to "
            f"kilometres at {MILES_TO_KM} km/mile. The source values are "
            f"unchanged."
        )
    if modes_read:
        notes.append(
            "Transport mode read from the upload for "
            f"{sum(modes_read.values())} lane(s): "
            + ", ".join(f"{m} ({n})" for m, n in sorted(modes_read.items()))
            + "."
        )
    elif lanes:
        notes.append(
            "No transport-mode column was recognised on the lanes sheet, so no "
            "lane carries a mode. Mode-sensitive cost and emissions figures are "
            "reported as unavailable rather than assumed to be road freight."
        )

    # ---- Lane rates (normalised freight table) -----------------------
    # Rate_Per_Unit lives per (Lane_ID, Product_ID). The lane carries the mean
    # across products so a single-product network is exact and a multi-product
    # one is explicitly averaged; the per-product rates are kept for the
    # assembler, which builds one lane per product.
    rates_by_lane: Dict[str, Dict[str, float]] = {}
    currencies: set = set()
    #: The money unit every cost in this network is stated in.
    #:
    #: The workbook says so — the US file's rates table carries `Currency=USD`
    #: on all 268 rows — and this value was detected here and then thrown away.
    #: Every downstream layer instead hardcoded INR: the metric registry's
    #: `unit="INR"`, the evidence formatter's `₹`, and the browser's
    #: `formatCurrency`. A USD network's ₹23,226,260 baseline was not a
    #: formatting slip; it was the wrong unit on an authoritative figure.
    currency: Optional[str] = None
    currency_basis: str = ""
    for _, df in sheets("lane_rates"):
        cl = {str(c).strip().lower(): c for c in df.columns}
        lane_col = _pick(cl, *LANE_ID_COLS)
        prod_col = _pick(cl, *PRODUCT_ID_COLS)
        rate_col = _pick(cl, *RATE_COLS)
        currency_col = _pick(cl, *CURRENCY_COLS)
        for _, row in df.iterrows():
            lid, rate = _text(row, lane_col), _num(row, rate_col)
            if not lid or rate is None:
                continue
            rates_by_lane.setdefault(lid, {})[_text(row, prod_col) or "*"] = rate
            if currency_col is not None:
                ccy = _text(row, currency_col).upper()
                if ccy:
                    currencies.add(ccy)

    # The optimiser has one money unit. A rates table quoting two currencies
    # would be summed as if they were the same number, so it is reported rather
    # than converted at a rate nobody supplied.
    if len(currencies) > 1:
        notes.append(
            f"Freight rates are quoted in more than one currency "
            f"({', '.join(sorted(currencies))}). They are summed as-is; no "
            f"exchange rate was supplied, and guessing one would change the "
            f"optimal answer."
        )
    elif len(currencies) == 1:
        currency = next(iter(currencies))
        currency_basis = "stated by the freight rates table"
        notes.append(
            f"Money in this network is {currency}, as stated by the freight "
            f"rates table. Every cost figure is reported in {currency}."
        )

    if currency is None:
        # Second-best evidence: a money column that names its unit in the
        # header, as `Monthly_Cost_USD` and `Held_Units_Value_USD` do. Only
        # accepted when every such header agrees; two disagreeing suffixes are
        # no better than none.
        suffixed: set = set()
        for _label, df in tables.items():
            for col in df.columns:
                m = re.search(r"_([A-Z]{3})$", str(col).strip())
                if m and m.group(1) in _ISO_CURRENCIES:
                    suffixed.add(m.group(1))
        if len(suffixed) == 1:
            currency = next(iter(suffixed))
            currency_basis = "named in a money column header"
            notes.append(
                f"Money in this network is {currency}, taken from column "
                f"headers that name the unit. No rates table stated a currency."
            )
        else:
            notes.append(
                "No column in this upload states a currency, so cost figures "
                "are reported as amounts without a currency symbol rather than "
                "labelled with a unit the data does not support."
            )

    if rates_by_lane:
        priced = 0
        for lane in lanes:
            per_product = rates_by_lane.get(lane.get("laneId") or "")
            if not per_product:
                continue
            lane["ratesByProduct"] = per_product
            if lane.get("cost") is None:
                lane["cost"] = sum(per_product.values()) / len(per_product)
            priced += 1
        notes.append(
            f"Freight rates joined from a separate rates table for {priced} of "
            f"{len(lanes)} lane(s), keyed by lane id."
        )

    # ---- Warehouse costs ---------------------------------------------
    # A per-site cost table, one row per cost line (RENT, LABOR, UTILITIES…),
    # each stating what that line costs per unit handled. The total handling
    # cost of a unit is the sum of those lines.
    #
    # Two things this must get right on real client data, both of which the US
    # workbook exercises deliberately:
    #
    #   * the same cost line restated at a later effective date supersedes the
    #     earlier one — it is a rent review, not a second rent;
    #   * identical rows repeated (F006's RENT appears ten times) are one fact
    #     recorded ten times, not ten costs. Summing them gave F006 a handling
    #     cost of 39.69/unit against a true 9.84 — four times its real cost, on
    #     a figure the optimiser uses to choose which site handles a unit.
    #
    # So rows are reduced to one per (facility, cost type), keeping the latest
    # effective date, before anything is added up.
    handling_by_facility: Dict[str, Dict[str, Tuple[str, float]]] = {}
    monthly_by_facility: Dict[str, Dict[str, Tuple[str, float]]] = {}
    wc_rows = 0
    for _, df in sheets("warehouse_costs"):
        cl = {str(c).strip().lower(): c for c in df.columns}
        fac_col = _pick(cl, *FACILITY_ID_COLS)
        type_col = _pick(cl, *COST_TYPE_COLS)
        unit_col = _pick(cl, *HANDLING_COLS)
        monthly_col = _pick(cl, "monthly_cost", "monthly_cost_usd", "monthly_cost_inr",
                            "cost_per_month", "monthly_amount")
        date_col = _pick(cl, *EFFECTIVE_DATE_COLS)
        for _, row in df.iterrows():
            fid = _text(row, fac_col)
            if not fid:
                continue
            wc_rows += 1
            # A table with no cost-type column states one line per site.
            line = _text(row, type_col).upper() or "TOTAL"
            eff = _text(row, date_col)  # ISO dates sort lexicographically
            per_unit = _num(row, unit_col)
            monthly = _num(row, monthly_col)
            if per_unit is not None:
                prev = handling_by_facility.setdefault(fid, {}).get(line)
                if prev is None or eff >= prev[0]:
                    handling_by_facility[fid][line] = (eff, per_unit)
            if monthly is not None:
                prev_m = monthly_by_facility.setdefault(fid, {}).get(line)
                if prev_m is None or eff >= prev_m[0]:
                    monthly_by_facility[fid][line] = (eff, monthly)

    if handling_by_facility:
        applied = 0
        for node in plants + dcs:
            lines = handling_by_facility.get(node["id"])
            if not lines:
                continue
            # Only fills a gap. A handling cost stated on the facilities sheet
            # is the client's own consolidated figure and wins.
            if node.get("handlingCost") is None:
                node["handlingCost"] = round(sum(v for _d, v in lines.values()), 4)
                node["handlingCostLines"] = {k: v for k, (_d, v) in lines.items()}
                applied += 1
        if applied:
            notes.append(
                f"Handling cost per unit for {applied} site(s) built from the "
                f"warehouse cost table: the sum of its cost lines "
                f"({', '.join(sorted({l for v in handling_by_facility.values() for l in v}))[:120]}), "
                f"taking the latest effective date where a line is restated. "
                f"{wc_rows} row(s) were read."
            )
    if monthly_by_facility:
        for node in plants + dcs:
            lines = monthly_by_facility.get(node["id"])
            if lines:
                node["operatingCostPerMonth"] = round(
                    sum(v for _d, v in lines.values()), 2)

    # ---- Demand history ----------------------------------------------
    # The client's demand is a monthly series per market and product. The
    # network's *current* demand is the latest period on record — never a
    # default, and never an average dressed up as an observation.
    for _, df in sheets("demand_history"):
        cl = {str(c).strip().lower(): c for c in df.columns}
        period_col = _pick(cl, *PERIOD_COLS)
        mkt_col = _pick(cl, *MARKET_ID_COLS)
        prod_col = _pick(cl, *PRODUCT_ID_COLS)
        qty_col = _pick(cl, *DEMAND_COLS)
        for _, row in df.iterrows():
            m_id, qty = _text(row, mkt_col), _num(row, qty_col)
            if not m_id or qty is None:
                continue
            demand_history.append({
                "period": _text(row, period_col),
                "marketId": m_id,
                "productId": _text(row, prod_col) or None,
                "quantity": qty,
            })

    if demand_history:
        latest = max(d["period"] for d in demand_history if d["period"])
        current: Dict[str, float] = {}
        for d in demand_history:
            if d["period"] == latest:
                current[d["marketId"]] = current.get(d["marketId"], 0.0) + d["quantity"]

        # A market seen only in the history still belongs to the network.
        for m_id in sorted(current):
            if m_id not in seen_markets:
                seen_markets.add(m_id)
                lat, lng, reg = lookup_coordinates(m_id, len(markets))
                markets.append({
                    "id": m_id, "name": m_id, "city": None, "state": None,
                    "lat": lat, "lng": lng, "demand": None, "slaDays": None,
                    "priority": None, "region": reg, "coordsExact": False,
                })
                notes.append(
                    f"Market {m_id} appears in the demand history but not in any "
                    f"markets sheet; it was added with an approximate position."
                )

        filled = 0
        for m in markets:
            if m.get("demand") is None and m["id"] in current:
                m["demand"] = current[m["id"]]
                filled += 1
        notes.append(
            f"Current demand for {filled} market(s) taken from the latest period "
            f"on record ({latest}) in the demand history — {len(demand_history)} "
            f"observations across "
            f"{len({d['period'] for d in demand_history})} periods."
        )

    # Market priority was assigned here as "High" above 2,500 units and
    # "Medium" below it — a threshold that appears in no upload and means
    # nothing to a network whose demand is measured in pallets, tonnes or tens
    # of thousands. Priority is a commercial judgement the client makes, so it
    # is read from their file when they state it and left unset when they do
    # not. `DemandRecord.priority` (which the optimiser uses to rank shortages)
    # is untouched by this and keeps its own default.
    for m in markets:
        if not m.get("priority"):
            m["priority"] = None

    # ---- Capacity history --------------------------------------------
    for _, df in sheets("capacity_history"):
        cl = {str(c).strip().lower(): c for c in df.columns}
        fac_col = _pick(cl, *FACILITY_ID_COLS)
        period_col = _pick(cl, *PERIOD_COLS)
        avail_col = _pick(cl, "available_capacity_units", "available_capacity", "capacity_units")
        used_col = _pick(cl, "used_capacity_units", "used_capacity", "utilised_capacity")
        for _, row in df.iterrows():
            fid = _text(row, fac_col)
            if not fid:
                continue
            capacity_history.append({
                "facilityId": fid,
                "period": _text(row, period_col),
                "available": _num(row, avail_col),
                "used": _num(row, used_col),
            })

    # ---- External signals --------------------------------------------
    for _, df in sheets("signals"):
        cl = {str(c).strip().lower(): c for c in df.columns}
        for _, row in df.iterrows():
            sid = _text(row, _pick(cl, "signal_id", "id"))
            if not sid:
                continue
            signals.append({
                "id": sid,
                "date": _text(row, _pick(cl, "signal_date", "date")) or None,
                "type": _text(row, _pick(cl, "signal_type", "type")) or None,
                "marketId": _text(row, _pick(cl, *MARKET_ID_COLS)) or None,
                "description": _text(row, _pick(cl, "description", "detail")) or None,
                "relevance": _text(row, _pick(cl, "relevance", "severity")) or None,
                "probability": _num(row, _pick(cl, "event_probability", "probability")),
            })

    unknown = [name for name, _ in sheets("unknown")]
    if unknown:
        notes.append(
            "These sheets were parsed but not recognised as any known table, so "
            "nothing was read from them: " + ", ".join(unknown[:8]) + "."
        )

    # REMOVED IN PHASE 10.0 — fabricated fallback network.
    #
    # When no plants, DCs or markets were recognised, this invented a complete
    # network — a "Primary Manufacturing Plant" at Baddi, two DCs at Delhi NCR
    # and Mumbai with utilisation figures of 81.6% and 72.0%, and three markets
    # with demand — and returned it as though it had been parsed from the
    # user's upload. A user whose spreadsheet did not match the expected schema
    # would have been shown a full analysis of a network they never provided.
    #
    # Nothing is substituted now. An upload that yields no facilities returns
    # none, and `assemble_network_from_structure` reports exactly what was
    # missing.

    # De-overlap co-located nodes so map markers stay separately clickable.
    # A plant and the market it serves in the same city legitimately share a
    # coordinate. The original `lat`/`lng` are preserved as `latSource`/
    # `lngSource`; only the display position moves, and the radius is 0.12°
    # (~13km) rather than the 0.6° (~65km) it used to be — that was large
    # enough to put the Mumbai plant offshore.
    node_groups: Dict[str, List[Dict[str, Any]]] = {}
    for n in plants + dcs + markets:
        k = f"{round(n['lat'], 3)},{round(n['lng'], 3)}"
        node_groups.setdefault(k, []).append(n)

    fan_r = 0.12
    fanned = 0
    for group in node_groups.values():
        if len(group) > 1:
            for i, node in enumerate(group):
                node["latSource"], node["lngSource"] = node["lat"], node["lng"]
                ang = (2 * math.pi * i) / len(group)
                node["lat"] = round(node["lat"] + fan_r * math.sin(ang), 4)
                node["lng"] = round(node["lng"] + fan_r * math.cos(ang), 4)
                fanned += 1
    if fanned:
        notes.append(
            f"{fanned} node(s) share a location with another node; their map "
            f"markers are spread by ~13km so each stays selectable. The "
            f"uploaded coordinates are unchanged."
        )

    # NOTE (Phase 10.0): this function previously called
    # `solve_extracted_network()` here and returned its output as `kpis`.
    # That path was a SECOND, independent MILP — a parallel `pulp.LpProblem`
    # with invented freight rates (`dist * 0.02`), straight-line distances
    # (`hypot(dlat, dlng) * 111`), no inventory/carbon/SLA/fixed-cost terms,
    # and a return payload that hardcoded `fillRate: 100.0` and
    # `slaAdherence: 96.5` regardless of the network. It bypassed
    # `netgravity/optimization/milp.py` entirely.
    #
    # Structural extraction stays (it is genuinely useful for mapping review);
    # optimisation does not happen here. KPIs come from the real engine, via
    # the orchestrator, once the network is bound to a project.
    integrity = _check_referential_integrity(
        tables, plants=plants, dcs=dcs, markets=markets,
        lanes=lanes, products=products,
        defined_lane_ids=defined_lane_ids,
    )
    for problem in integrity:
        notes.append(problem["detail"])

    geography = infer_geography(plants + dcs + markets)

    return {
        "plants": plants,
        "dcs": dcs,
        "markets": markets,
        "lanes": lanes,
        "products": products,
        "demandHistory": demand_history,
        "capacityHistory": capacity_history,
        "signals": signals,
        "notes": notes,
        #: The money unit, from the upload. None when nothing states one.
        "currency": currency,
        "currencyBasis": currency_basis,
        #: Where the network is, inferred from its own coordinates.
        "geography": geography,
        #: Cross-sheet foreign keys that point at nothing. Reported before
        #: confirmation so a user can fix the file, rather than discovered as a
        #: silently dropped row after the solve.
        "integrity": integrity,
    }


#: Which id column of which sheet role must exist in which collection.
_FOREIGN_KEYS: Tuple[Tuple[str, Tuple[str, ...], str, str], ...] = (
    ("lane_rates", LANE_ID_COLS, "lanes", "lane"),
    ("lane_rates", PRODUCT_ID_COLS, "products", "product"),
    ("demand_history", MARKET_ID_COLS, "markets", "market"),
    ("demand_history", PRODUCT_ID_COLS, "products", "product"),
    ("capacity_history", FACILITY_ID_COLS, "facilities", "facility"),
    ("warehouse_costs", FACILITY_ID_COLS, "facilities", "facility"),
    ("signals", MARKET_ID_COLS, "markets", "market"),
    ("lanes", ("origin_id", "from", "origin", "source_id"), "nodes", "origin"),
    ("lanes", ("destination_id", "to", "destination", "dest_id"), "nodes", "destination"),
)


def _check_referential_integrity(
    tables: Dict[str, pd.DataFrame], *,
    plants: List[Dict[str, Any]], dcs: List[Dict[str, Any]],
    markets: List[Dict[str, Any]], lanes: List[Dict[str, Any]],
    products: List[Dict[str, Any]],
    defined_lane_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """
    Find ids that reference a record no sheet defines.

    A workbook can be 99.9% "valid" by row-completeness and still be wrong in
    the way that matters: the US test file carries two `Transportation_Rates`
    rows priced against lane `L099`, which appears in no lanes sheet. Those
    rows are silently dropped by the join — the freight they price never
    reaches the model, and nothing said so. Row-level validity cannot see this;
    only a cross-sheet key check can.

    Returns one entry per (sheet, column, missing-id-set), each carrying the
    offending ids so the user can find them in their own file.
    """
    known: Dict[str, set] = {
        "lanes": (set(defined_lane_ids) if defined_lane_ids
                  else {str(l.get("laneId")) for l in lanes if l.get("laneId")}),
        "products": {str(p.get("id")) for p in products if p.get("id")},
        "markets": {str(m.get("id")) for m in markets if m.get("id")},
        "facilities": {str(f.get("id")) for f in (plants + dcs) if f.get("id")},
    }
    known["nodes"] = known["facilities"] | known["markets"]

    problems: List[Dict[str, Any]] = []
    for label, df in tables.items():
        role = classify_sheet(df)
        cl = {str(c).strip().lower(): c for c in df.columns}
        for fk_role, id_cols, target, noun in _FOREIGN_KEYS:
            if role != fk_role:
                continue
            col = _pick(cl, *id_cols)
            if col is None or not known.get(target):
                continue
            referenced = {
                str(v).strip() for v in df[col].dropna().unique() if str(v).strip()
            }
            missing = sorted(referenced - known[target])
            if not missing:
                continue
            rows = int(df[col].astype(str).str.strip().isin(missing).sum())
            shown = ", ".join(missing[:6]) + ("…" if len(missing) > 6 else "")
            problems.append({
                "type": "Orphan reference",
                "severity": "error",
                "table": label,
                "column": str(col),
                "count": rows,
                "missingIds": missing[:50],
                "detail": (
                    f"{rows} row(s) in '{label}' reference a {noun} that no "
                    f"sheet defines: {shown}. Those rows cannot be joined and "
                    f"are not read by the model."
                ),
            })
    return problems


# ---------------------------------------------------------------------------
# REMOVED IN PHASE 10.0 — duplicate optimisation engine
# ---------------------------------------------------------------------------
# `solve_extracted_network()` lived here: a second, self-contained PuLP MILP
# that solved uploaded networks independently of `netgravity/optimization/milp.py`.
#
# It was removed rather than repaired because it was not a variant of the real
# solver, it was a different model answering a different question with invented
# inputs:
#   * freight cost invented as `max(5.0, dist * 0.02)`;
#   * distance invented as `hypot(dlat, dlng) * 111` (degrees, not road km);
#   * lead time invented as `dist / 600`;
#   * no inventory, carbon, SLA, facility-fixed-cost or shortage terms;
#   * returned `fillRate: 100.0` and `slaAdherence: 96.5` as LITERALS,
#     never computed, and reported `status: "FEASIBLE"` even when the solve
#     was not.
#
# Optimisation is authoritative and singular (brief §5, §10): it belongs to
# `netgravity/optimization/milp.py`, reached through the orchestrator's
# `optimization.solve` capability, on a CanonicalNetwork registered as a
# snapshot. See `app/backend/services/project_registry.py`.
