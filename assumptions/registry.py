"""
NetGravity — Explicit Assumption Registry
==========================================
Every modeling assumption is registered here with:
  - id
  - description
  - default_value
  - unit
  - rationale
  - source
  - confidence
  - whether user can override
  - which module it affects

PURPOSE: Auditability. A supply-chain consultant must be able to
explain every equation and every assumption to a client.

Do NOT hide assumptions inside equations.
Do NOT use assumptions without documenting them here.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Assumption:
    """
    A single registered modeling assumption.

    Attributes:
        assumption_id:     Unique identifier (e.g., "A-001")
        description:       Human-readable statement of the assumption
        default_value:     Default value used in the model
        unit:              Physical unit or "dimensionless"
        rationale:         Why this assumption was made
        source:            Literature or standard reference
        confidence:        HIGH / MEDIUM / LOW
        user_overridable:  Can a user override this via config?
        module:            Which module uses this assumption
        caveats:           Known limitations or scenarios where assumption fails
    """
    assumption_id:    str
    description:      str
    default_value:    Any
    unit:             str       = "dimensionless"
    rationale:        str       = ""
    source:           str       = ""
    confidence:       str       = "MEDIUM"     # HIGH / MEDIUM / LOW
    user_overridable: bool      = True
    module:           str       = "general"
    caveats:          List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ASSUMPTION_REGISTRY: List[Assumption] = [

    Assumption(
        assumption_id   = "A-001",
        description     = "Demand at each market follows a Normal distribution",
        default_value   = "NORMAL",
        unit            = "distribution_type",
        rationale       = (
            "Normal distribution is the standard assumption in safety-stock "
            "formulas (Chopra & Meindl §12.3). It is analytically tractable "
            "and provides a closed-form safety-stock formula."
        ),
        source          = "Chopra & Meindl, SCM 5th Ed., Chapter 12",
        confidence      = "MEDIUM",
        user_overridable = True,
        module          = "inventory/module.py",
        caveats         = [
            "May not hold for low-volume / intermittent demand (use Poisson or Gamma)",
            "Does not capture fat tails or demand spikes",
            "Correlation between markets is ignored in V1",
        ],
    ),

    Assumption(
        assumption_id   = "A-002",
        description     = "Planning horizon is a single period (T=1)",
        default_value   = 1,
        unit            = "periods",
        rationale       = (
            "Single-period model is standard for strategic network design "
            "(Chopra & Meindl §5.3). Multi-period adds T×|W| binary variables "
            "and is deferred to V2."
        ),
        source          = "Chopra & Meindl, SCM 5th Ed., Chapter 5",
        confidence      = "HIGH",
        user_overridable = False,
        module          = "optimization/milp.py",
        caveats         = [
            "Cannot capture seasonal demand variation",
            "Cannot model phased facility opening",
            "Multi-period extension point documented in architecture",
        ],
    ),

    Assumption(
        assumption_id   = "A-003",
        description     = "Transport cost is linear in flow volume",
        default_value   = True,
        unit            = "boolean",
        rationale       = (
            "Linear transport cost (rate × volume) is standard in MILP network "
            "design models (Chopra & Meindl §5.3). Economies of scale / "
            "fixed-trip costs can be added via CostEngine without MILP restructure."
        ),
        source          = "Chopra & Meindl, SCM 5th Ed., Chapter 5",
        confidence      = "HIGH",
        user_overridable = False,
        module          = "costs/engine.py",
        caveats         = [
            "Does not capture volume discounts / TL vs LTL distinction",
            "Fixed trip cost (full-truck-load) requires additional binary variable",
            "CostEngine is pluggable for future nonlinear models",
        ],
    ),

    Assumption(
        assumption_id   = "A-004",
        description     = "A facility is available at full capacity immediately upon opening",
        default_value   = True,
        unit            = "boolean",
        rationale       = (
            "Single-period model has no concept of ramp-up. "
            "Ramp-up costs and timing are captured as implementation parameters "
            "in transition cost config, not in the MILP itself."
        ),
        source          = "Implementation guidance — faculty advisor",
        confidence      = "HIGH",
        user_overridable = False,
        module          = "optimization/milp.py",
        caveats         = [
            "Real facilities take time to reach rated capacity",
            "Ramp-up should be modeled in multi-period extension",
        ],
    ),

    Assumption(
        assumption_id   = "A-005",
        description     = "Carbon accounting covers transport flows only (not facility operations)",
        default_value   = "TRANSPORT_ONLY",
        unit            = "scope",
        rationale       = (
            "Transport-related emissions are the primary Scope 3 supply chain "
            "emission source. Facility scope 1/2 emissions require energy audit "
            "data not available in V1."
        ),
        source          = "GLEC Framework v2.0; GHG Protocol Scope 3",
        confidence      = "HIGH",
        user_overridable = True,
        module          = "carbon/module.py",
        caveats         = [
            "Facility electricity/heat emissions not included",
            "Upstream supplier emissions not included",
            "Packaging material emissions not included",
        ],
    ),

    Assumption(
        assumption_id   = "A-006",
        description     = "Warehouse/DC nodes are pure flow-through (inbound = outbound)",
        default_value   = "FLOW_THROUGH",
        unit            = "balance_mode",
        rationale       = (
            "Flow conservation at intermediate nodes is the standard assumption "
            "in transshipment network models (Chopra & Meindl §5.3). "
            "Inventory is modeled separately via the InventoryModule as a cost, "
            "not as a stock variable in V1."
        ),
        source          = "Chopra & Meindl, SCM 5th Ed., Chapter 5",
        confidence      = "HIGH",
        user_overridable = False,
        module          = "optimization/milp.py",
        caveats         = [
            "Does not model time-of-stock at DC",
            "Inventory variable I_{ikt} is deferred to multi-period V2",
        ],
    ),

    Assumption(
        assumption_id   = "A-007",
        description     = "Safety stock z-score defaults to 1.645 (95% cycle service level)",
        default_value   = 1.645,
        unit            = "z_score",
        rationale       = (
            "Industry standard for high-service-level B2B distribution. "
            "95% CSL means 5% probability of stockout in a replenishment cycle."
        ),
        source          = "Chopra & Meindl, SCM 5th Ed., §12.3; Normal distribution table",
        confidence      = "HIGH",
        user_overridable = True,
        module          = "inventory/module.py",
        caveats         = [
            "CSL is cycle service level, not fill rate — different metric",
            "z=1.645 assumes Normal distribution (see A-001)",
        ],
    ),

    Assumption(
        assumption_id   = "A-008",
        description     = "By default, all demand must be fully met (no unmet demand)",
        default_value   = False,   # allow_shortage = False
        unit            = "boolean",
        rationale       = (
            "Hard demand satisfaction is the standard formulation for strategic "
            "network design where losing customers is not acceptable. "
            "Shortage variables can be enabled via config for stress-testing."
        ),
        source          = "Faculty advisor guidance; Chopra & Meindl §5.3",
        confidence      = "HIGH",
        user_overridable = True,
        module          = "optimization/milp.py",
        caveats         = [
            "May make model infeasible if network capacity is insufficient",
            "Enable allow_shortage=True for disruption/stress scenarios",
        ],
    ),

    Assumption(
        assumption_id   = "A-009",
        description     = "Emission factors are homogeneous within each transport mode",
        default_value   = "HOMOGENEOUS",
        unit            = "mode_level_ef",
        rationale       = (
            "Lane-level emission factors require vehicle-specific data not "
            "available in V1. Mode-level EFs from GLEC Framework are used as "
            "standard proxies."
        ),
        source          = "GLEC Framework v2.0, Table 5",
        confidence      = "MEDIUM",
        user_overridable = True,
        module          = "carbon/module.py",
        caveats         = [
            "Does not capture fleet age, fuel type, load factor differences",
            "Lane-level EF override is supported via LaneRecord.emission_factor_override",
        ],
    ),

    Assumption(
        assumption_id   = "A-010",
        description     = "Safety stock assumes demand independence across markets",
        default_value   = "INDEPENDENT",
        unit            = "correlation_assumption",
        rationale       = (
            "Demand correlation between markets would require a covariance matrix "
            "and changes the portfolio aggregation. V1 uses the square-root rule "
            "which assumes independence."
        ),
        source          = "Chopra & Meindl, SCM 5th Ed., §12.4 (Risk Pooling)",
        confidence      = "MEDIUM",
        user_overridable = False,
        module          = "inventory/module.py",
        caveats         = [
            "Positive correlation means actual variability is higher than calculated",
            "Negative correlation means actual variability is lower than calculated",
            "Inventory pooling benefits may be overstated",
        ],
    ),

    Assumption(
        assumption_id   = "A-011",
        description     = "CoG / Weiszfeld result is a geographic screening output only",
        default_value   = "SCREENING_ONLY",
        unit            = "decision_type",
        rationale       = (
            "Gravity models minimize weighted distance but ignore fixed costs, "
            "capacity, service requirements and real transport networks. "
            "Faculty advisor explicitly directed: CoG is not the final answer. "
            "See docs/model_foundation.md §1.1"
        ),
        source          = "Faculty advisor guidance; Chopra & Meindl §5.2",
        confidence      = "HIGH",
        user_overridable = False,
        module          = "cog/screener.py",
        caveats         = [
            "CoG result must be mapped to real candidate sites before MILP solve",
            "MILP may select a different facility if cost/capacity warrants",
        ],
    ),
]

# ---------------------------------------------------------------------------
# Registry access
# ---------------------------------------------------------------------------

def get_assumption(assumption_id: str) -> Optional[Assumption]:
    """Retrieve an assumption by ID."""
    for a in ASSUMPTION_REGISTRY:
        if a.assumption_id == assumption_id:
            return a
    return None


def get_assumptions_by_module(module: str) -> List[Assumption]:
    """Retrieve all assumptions for a given module."""
    return [a for a in ASSUMPTION_REGISTRY if a.module == module]


def get_low_confidence_assumptions() -> List[Assumption]:
    """Return assumptions with LOW or MEDIUM confidence for review."""
    return [a for a in ASSUMPTION_REGISTRY if a.confidence in ("LOW", "MEDIUM")]


def print_assumption_report() -> None:
    """Print a formatted assumption report for audit purposes."""
    print("=" * 70)
    print("NETGRAVITY — ASSUMPTION REGISTRY")
    print("=" * 70)
    for a in ASSUMPTION_REGISTRY:
        print(f"\n[{a.assumption_id}] {a.description}")
        print(f"  Default:     {a.default_value} ({a.unit})")
        print(f"  Module:      {a.module}")
        print(f"  Confidence:  {a.confidence}")
        print(f"  Overridable: {a.user_overridable}")
        print(f"  Source:      {a.source}")
        if a.caveats:
            print(f"  Caveats:")
            for c in a.caveats:
                print(f"    • {c}")


# Optional type
from typing import Optional
