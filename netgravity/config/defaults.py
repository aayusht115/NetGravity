"""
NetGravity — Default Configuration Parameters
=============================================
All configurable defaults. Override via OptimizationConfig or environment.

IMPORTANT: Do not hard-code client data here.
These are engineering defaults for the optimizer itself.
"""

from typing import Dict

# ---------------------------------------------------------------------------
# Solver defaults
# ---------------------------------------------------------------------------

SOLVER_DEFAULTS: Dict = {
    "solver_name":        "HiGHS",    # HiGHS via PuLP
    "time_limit_seconds": 300,
    "mip_gap":            0.001,      # 0.1% optimality gap
    "threads":            0,          # 0 = auto-detect
    "verbose":            False,
}

# ---------------------------------------------------------------------------
# Model defaults
# ---------------------------------------------------------------------------

MODEL_DEFAULTS: Dict = {
    "model_version":   "1.2.0",
    "cost_period":     "MONTH",
    "objective_mode":  "COST_MIN",
    "allow_shortage":  False,
    "shortage_penalty": 1_000_000.0,  # very high penalty to effectively force full coverage
    "enable_inventory": True,
    "enable_carbon_cost": False,
    "carbon_price":    0.0,
    "sourcing_policy": "MULTI",
    "service_metric":  "TRANSIT_TIME",
    "enforce_sla":     True,
}

# ---------------------------------------------------------------------------
# Inventory defaults
# ---------------------------------------------------------------------------

INVENTORY_DEFAULTS: Dict = {
    "z_score_95_csl":  1.645,   # 95% cycle service level
    "z_score_99_csl":  2.326,   # 99% cycle service level
    "z_score_90_csl":  1.282,   # 90% cycle service level
    "default_csl":     0.95,
    "distribution":    "NORMAL",  # see assumption A-001
    "holding_rate":    0.25,      # 25% of unit value per year
}

# ---------------------------------------------------------------------------
# Carbon emission factors by transport mode
# (kg CO₂ per tonne · km — standard GLEC Framework defaults)
# Source: Global Logistics Emissions Council (GLEC) Framework v2.0
# ASSUMPTION A-010: homogeneous within mode
# ---------------------------------------------------------------------------

EMISSION_FACTORS_KG_CO2_PER_TONNE_KM: Dict[str, float] = {
    "ROAD":       0.062,   # average road freight (truck)
    "RAIL":       0.028,   # average rail freight
    "AIR":        0.602,   # air freight
    "SEA":        0.019,   # container ship
    "INTERMODAL": 0.035,   # weighted average road/rail
}

# ---------------------------------------------------------------------------
# Utilization thresholds (for analytics)
# ---------------------------------------------------------------------------

UTILIZATION_THRESHOLDS: Dict = {
    "over_threshold":  0.90,   # > 90% = overutilized
    "under_threshold": 0.30,   # < 30% = underutilized
}

# ---------------------------------------------------------------------------
# Service: when demand coverage is a finding in its own right
# ---------------------------------------------------------------------------
# The line below which a plan's demand coverage must be SAID, however good its
# cost is. A cost ranking answers one question; a plan that costs less while
# stranding demand is cheaper and not therefore better, and a card headed with
# the cost alone invites exactly that reading.
#
# Declared here rather than in the screen that shows it, so the warning and
# any engine check draw the line in the same place.

SERVICE_THRESHOLDS: Dict = {
    "fill_rate_floor": 0.95,   # < 95% of demand served => say so, prominently
}

# ---------------------------------------------------------------------------
# Flow analytics: top-N defaults
# ---------------------------------------------------------------------------

ANALYTICS_DEFAULTS: Dict = {
    "top_corridors_n": 10,      # top N corridors by volume/cost
    "min_flow_display": 0.01,   # ignore flows below this (rounding artifact)
}

# ---------------------------------------------------------------------------
# Go/No-Go thresholds (configurable)
# ---------------------------------------------------------------------------

GO_NO_GO_DEFAULTS: Dict = {
    "savings_threshold":      0.0,    # minimum annual savings to be a "GO"
    "service_delta_threshold": -0.02, # service may not drop more than 2%
    "carbon_weight":           0.0,   # default: carbon not in go/no-go
}

# ---------------------------------------------------------------------------
# Emission factor metadata (V1.1 — for audit traceability)
# ---------------------------------------------------------------------------
# This metadata is recorded in results alongside the factor values.
# Update when factors are revised or a new methodology is adopted.

EMISSION_FACTOR_METADATA: Dict = {
    "methodology":        "GLEC Framework",
    "version":            "v2.0",
    "identifier":         "GLEC_v2.0",
    "effective_date":     "2019-01-01",
    "source":             "Global Logistics Emissions Council (GLEC), https://www.smartfreightcentre.org/en/",
    "scope":              "Well-to-wheel (WtW) GHG emissions from freight transportation",
    "unit":               "kg CO2-eq per tonne·km",
    "notes": (
        "Default factors. Override via OptimizationConfig.emission_factor_table. "
        "Lane-level overrides take precedence over config-level table, "
        "which takes precedence over these defaults."
    ),
}
