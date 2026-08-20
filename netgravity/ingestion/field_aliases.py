"""
NetGravity — Client Field Name Aliases
=======================================
Maps the CLIENT-FACING field names from NetGravity_Input_Data_Fields.xlsx
onto the engine's internal Pydantic field names.

WHY THIS EXISTS
---------------
The Excel workbook is the specification we hand a client: "send us these
fields." It uses names like `Facility_ID`, `Capacity_Units` and
`Fixed_Annual_Cost`. The engine's schema (netgravity/schemas/network.py) uses
`id`, `capacity_units_per_period` and `fixed_cost_per_year`.

Without a translation layer, data arriving in exactly the format we asked for
would fail to load — which would be an absurd own goal, since closing that gap
is the entire reason the ingestion layer exists.

RULES
-----
1. The CLIENT name from the workbook is the contract. It always works.
2. The internal name also works, so existing files keep loading.
3. Matching is case-insensitive and ignores spaces, hyphens and underscores,
   so `Facility ID`, `facility-id` and `FACILITY_ID` all resolve.
4. Unknown columns are passed through untouched and simply ignored by the
   parser, rather than causing a failure.

When the workbook changes, update this file — not the parsers.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# --- canonical internal name -> every accepted spelling ---------------------
# Client-facing names (from the workbook) are listed first in each list.

FACILITY_ALIASES: Dict[str, List[str]] = {
    "facility_id":                  ["Facility_ID", "facility_id", "Node_ID", "Site_ID"],
    "facility_name":                ["Facility_Name", "facility_name", "Site_Name"],
    "role":                         ["Type", "Extended_Facility_Type", "role",
                                     "Facility_Type", "Node_Role"],
    "status":                       ["Status", "status", "Facility_Status"],
    "latitude":                     ["Latitude", "latitude", "Lat"],
    "longitude":                    ["Longitude", "longitude", "Lon", "Lng", "Long"],
    "city":                         ["City", "city", "Address"],
    "state":                        ["State", "state"],
    "region":                       ["Region", "region"],
    "country":                      ["Country", "country"],
    "capacity_units_per_period":    ["Capacity_Units", "capacity_units_per_period",
                                     "Capacity", "Throughput_Capacity"],
    "storage_capacity":             ["Storage_Capacity", "storage_capacity"],
    "fixed_cost_per_year":          ["Fixed_Annual_Cost", "fixed_cost_per_year",
                                     "Annual_Fixed_Cost"],
    "handling_cost_per_unit":       ["Variable_Handling_Cost_Per_Unit",
                                     "handling_cost_per_unit", "Handling_Cost"],
    "is_mandatory":                 ["Mandatory_Open_Flag", "is_mandatory",
                                     "Must_Stay_Open"],
    "is_closable":                  ["Closable_Flag", "is_closable"],
    "observed_throughput":          ["Observed_Throughput_Units", "observed_throughput",
                                     "Actual_Throughput"],
    "observed_utilization_pct":     ["Observed_Utilization_Pct", "observed_utilization_pct"],
    "replenishment_lead_time_days": ["Replenishment_Lead_Time",
                                     "replenishment_lead_time_days"],
    "capex":                        ["CapEx", "capex", "Capital_Expenditure"],
    "closure_cost":                 ["Closure_Cost", "closure_cost"],
    "opening_cost":                 ["Opening_Cost", "opening_cost"],
    "min_throughput_per_period":    ["Min_Throughput_Per_Period",
                                     "min_throughput_per_period"],
    "ownership_type":               ["Ownership_Type", "ownership_type"],
    "carbon_emission_factor":       ["Carbon_Emission_Factor", "carbon_emission_factor"],
}

MARKET_ALIASES: Dict[str, List[str]] = {
    "market_id":     ["Zone_ID", "market_id", "Market_ID", "Customer_ID"],
    "market_name":   ["Zone_Name", "market_name", "Market_Name"],
    "latitude":      ["Latitude", "latitude", "Lat"],
    "longitude":     ["Longitude", "longitude", "Lon", "Lng", "Long"],
    "region":        ["Region", "region"],
    "state":         ["State", "state"],
    "priority_tier": ["Priority_Tier", "priority_tier", "Priority"],
    "sla_days":      ["SLA_Requirement", "sla_days", "SLA_Days", "SLA"],
    # Demand may arrive on the zones sheet itself — see structured.py
    "quantity":      ["Daily_Demand_Units", "quantity", "Demand_Units", "Demand"],
    "std_dev":       ["Demand_Variability", "Std_Dev", "std_dev",
                      "Demand_Std_Dev"],
    "growth_rate_annual": ["Growth_Rate_Annual", "growth_rate_annual"],
    "product_mix":   ["Product_Mix", "product_mix"],
}

DEMAND_ALIASES: Dict[str, List[str]] = {
    "market_id":     ["Zone_ID", "market_id", "Market_ID"],
    "product_id":    ["Product_ID", "product_id", "SKU"],
    "period":        ["Period", "period", "Date"],
    "quantity":      ["Daily_Demand_Units", "quantity", "Demand_Units", "Demand"],
    "std_dev":       ["Demand_Variability", "Std_Dev", "std_dev"],
    "sla_days":      ["SLA_Requirement", "sla_days", "SLA_Days"],
    "service_level": ["Service_Level", "service_level", "CSL"],
    "priority":      ["Priority_Tier", "priority", "Priority"],
}

LANE_ALIASES: Dict[str, List[str]] = {
    "origin_id":               ["Origin_ID", "origin_id", "From", "Source_ID"],
    "destination_id":          ["Destination_ID", "destination_id", "To", "Dest_ID"],
    "mode":                    ["Mode", "mode", "Transport_Mode"],
    "rate_per_unit":           ["Unit_Cost", "rate_per_unit", "Cost_Per_Unit", "Rate"],
    "distance_km":             ["Distance_KM", "distance_km", "Distance"],
    "lead_time_days":          ["Lead_Time_Days", "lead_time_days", "Transit_Days"],
    "lane_capacity":           ["Capacity_Per_Trip", "lane_capacity", "Lane_Capacity"],
    "is_active_baseline":      ["Current_Lane_Flag", "is_active_baseline",
                                "Active_Flag"],
    "observed_current_volume": ["Observed_Current_Volume", "observed_current_volume",
                                "Current_Volume"],
    "fuel_surcharge_pct":      ["Fuel_Surcharge_Pct", "fuel_surcharge_pct"],
    "transit_frequency":       ["Transit_Frequency", "transit_frequency"],
    "min_load_quantity":       ["Min_Load_Quantity", "Full_Truck_Load_Threshold",
                                "min_load_quantity"],
    "emission_factor_override": ["Emission_Factor_Override", "emission_factor_override"],
}

PRODUCT_ALIASES: Dict[str, List[str]] = {
    "product_id":   ["Product_ID", "product_id", "SKU"],
    "product_name": ["Product_Name", "product_name", "Description"],
    "unit":         ["Unit", "unit", "UOM"],
    "weight_kg":    ["Weight", "weight_kg", "Unit_Weight", "Weight_KG"],
    "volume_m3":    ["Volume", "volume_m3", "Unit_Volume"],
    "unit_value":   ["Unit_Value", "unit_value", "Value"],
    "holding_rate": ["Holding_Rate", "holding_rate", "Carrying_Rate"],
}

HISTORY_ALIASES: Dict[str, List[str]] = {
    "node_id":             ["Node_ID", "node_id", "Lane_ID", "Facility_ID"],
    "period":              ["Date", "Period", "period"],
    "volume":              ["Volume", "volume", "Units"],
    "order_count":         ["Order_Count", "order_count", "Orders"],
    "promotion_flag":      ["Promotion_Flag", "promotion_flag"],
    "holiday_flag":        ["Holiday_Flag", "holiday_flag"],
    "returns_volume":      ["Returns_Volume", "returns_volume"],
    "observed_or_planned": ["Observed_or_Planned_Flag", "observed_or_planned"],
    "data_version":        ["Data_Version", "Load_Timestamp", "data_version"],
}


def _normalise(name: str) -> str:
    """Strip case and separators so 'Facility ID' == 'facility_id' == 'FACILITY-ID'."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


#: Public alias. The memory layer keys confirmed mappings on normalised
#: column names, and MUST use the identical rule the alias table uses — two
#: different normalisations would let the same column resolve one way against
#: the dictionary and another against memory.
normalise_name = _normalise


# ---------------------------------------------------------------------------
# Declared time period for ambiguous quantity columns
# ---------------------------------------------------------------------------
# DECISION (confirmed): the engine standardizes on MONTH. OptimizationConfig
# already defaults to cost_period=MONTH and days_per_period=30, and every
# per-period cost method on FacilityRecord/ProductRecord already normalizes
# to MONTH. Ingestion must match that, not invent a second convention.
#
# The workbook itself is not period-consistent: `Daily_Demand_Units` (a DAY
# figure) sits next to `Fixed_Annual_Cost` (already handled correctly via
# FacilityRecord.fixed_cost_per_year, which IS an annual field by design and
# is converted at solve time by get_fixed_cost_for_period()). Demand has no
# such conversion anywhere — a raw daily count was flowing straight into
# DemandRecord.quantity, which the engine treats as a MONTHLY figure. That
# understates monthly demand vs. monthly facility cost by ~30x, which biases
# the optimizer toward closing facilities that should stay open.
#
# This table maps the RAW client column name (not the canonical field name,
# which is period-agnostic) to the period its values are actually expressed
# in, so the conversion can be applied once, loudly, at ingestion time.
DAYS_PER_MONTH = 30   # must track OptimizationConfig.days_per_period default

# Period words that may appear in a client's column name, longest first so
# "monthly" is not shadowed by a shorter match. A column whose name says
# nothing about its period is assumed to be MONTH-native already, matching
# the engine convention — that assumption is what UNSTATED_PERIOD_FIELDS and
# the demand-vs-capacity consistency check below exist to protect.
_PERIOD_NAME_HINTS = (
    ("perannum", "YEAR"), ("annually", "YEAR"), ("annual", "YEAR"),
    ("yearly", "YEAR"), ("peryear", "YEAR"),
    ("quarterly", "QUARTER"), ("perquarter", "QUARTER"),
    ("monthly", "MONTH"), ("permonth", "MONTH"), ("mtd", "MONTH"),
    ("weekly", "WEEK"), ("perweek", "WEEK"),
    ("daily", "DAY"), ("perday", "DAY"),
)


def strip_period_tokens(normalised: str) -> str:
    """
    Remove a period word from an already-normalised name.

    'dailycapacityunits' -> 'capacityunits', so a client who states the period
    explicitly still matches the canonical field. Without this, qualifying a
    column name would make it silently unrecognised — the opposite of the
    intended effect, and exactly what R-021 asks people to do.
    """
    for token, _ in _PERIOD_NAME_HINTS:
        if token in normalised:
            candidate = normalised.replace(token, "", 1)
            if candidate:
                return candidate
    return normalised


def infer_period_from_name(name: object) -> Optional[str]:
    """
    Read the period out of a column name, e.g. 'Daily_Demand_Units' -> DAY.

    Returns None when the name says nothing — which is NOT the same as MONTH.
    Callers decide what an unstated period means, because that choice is
    exactly where the demand/capacity mismatch came from.
    """
    normalised = _normalise(name)
    for token, period in _PERIOD_NAME_HINTS:
        if token in normalised:
            return period
    return None

PERIOD_TO_MONTHLY_FACTOR: Dict[str, float] = {
    "DAY":     float(DAYS_PER_MONTH),
    "WEEK":    DAYS_PER_MONTH / 7.0,
    "MONTH":   1.0,
    "QUARTER": 1.0 / 3.0,
    "YEAR":    1.0 / 12.0,
}


def detect_native_period(rows: List[Dict[str, object]],
                         alias_map: Dict[str, List[str]],
                         canonical_field: str = "quantity",
                         default: Optional[str] = "MONTH") -> Optional[str]:
    """
    Look at the RAW (pre-rename) header to find which period the values in
    `canonical_field` were actually declared in — e.g. a column that arrives
    as 'Daily_Demand_Units' is DAY-native even though it renames to the
    period-agnostic canonical name 'quantity'.

    Must be called BEFORE rename_rows()/rename_row() overwrite the original
    header, since that is the only place the period information lives.

    Returns `default` when the column is absent or its name states no period.
    Pass default=None to distinguish "says nothing" from "says monthly".
    """
    if not rows:
        return default
    normalised_aliases = {_normalise(a) for a in alias_map.get(canonical_field, [])}
    for key in rows[0].keys():
        if key is None:
            continue
        normalised = _normalise(key)
        if normalised in normalised_aliases or \
                strip_period_tokens(normalised) in normalised_aliases:
            return infer_period_from_name(key) or default
    return default


def monthly_conversion_factor(period: str) -> float:
    """Multiply a `period`-native quantity by this to express it as MONTHLY."""
    return PERIOD_TO_MONTHLY_FACTOR.get(str(period).upper(), 1.0)


def build_lookup(alias_map: Dict[str, List[str]]) -> Dict[str, str]:
    """Turn {canonical: [aliases]} into {normalised_alias: canonical}."""
    lookup: Dict[str, str] = {}
    for canonical, aliases in alias_map.items():
        lookup[_normalise(canonical)] = canonical
        for alias in aliases:
            lookup[_normalise(alias)] = canonical
    return lookup


def rename_row(row: Dict[str, object], lookup: Dict[str, str]) -> Dict[str, object]:
    """
    Rewrite one row's keys to canonical names.

    Unrecognised columns are kept under their original key rather than dropped,
    so nothing is lost silently and parsers can ignore what they do not need.
    """
    out: Dict[str, object] = {}
    for key, value in row.items():
        if key is None:
            continue
        normalised = _normalise(key)
        canonical = lookup.get(normalised)
        if canonical is None:
            # Second chance: a period-qualified name such as
            # 'Monthly_Capacity_Units' still means Capacity_Units.
            stripped = strip_period_tokens(normalised)
            if stripped != normalised:
                canonical = lookup.get(stripped)
        out[canonical or key] = value
    return out


def rename_rows(rows, lookup: Dict[str, str]):
    return [rename_row(r, lookup) for r in rows]


# Pre-built lookups
FACILITY_LOOKUP = build_lookup(FACILITY_ALIASES)
MARKET_LOOKUP = build_lookup(MARKET_ALIASES)
DEMAND_LOOKUP = build_lookup(DEMAND_ALIASES)
LANE_LOOKUP = build_lookup(LANE_ALIASES)
PRODUCT_LOOKUP = build_lookup(PRODUCT_ALIASES)
HISTORY_LOOKUP = build_lookup(HISTORY_ALIASES)
