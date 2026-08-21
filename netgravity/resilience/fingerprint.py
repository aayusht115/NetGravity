"""
NetGravity — Material optimization fingerprint.

A content hash over ONLY the inputs that can change a MILP solution.

Why not reuse `CanonicalNetwork.compute_data_version()`? Because that hashes
every field of every record, including purely descriptive ones — `name`,
`region`, `country`, `tags`, `description`. Renaming a warehouse would change
that hash and needlessly invalidate an entire REI batch, forcing 1 + N solves
for a label edit.

This fingerprint answers a narrower question:

    "Could this change alter the optimal network cost?"

MATERIAL (invalidates REI)          NOT MATERIAL (does not invalidate)
──────────────────────────          ──────────────────────────────────
demand quantity / σ / SLA           facility display name
facility capacity & status          region / country / tags
facility open/close flags           network description
contractual closure terms           parameter provenance & confidence
fixed / handling / opening /        solver name, time limit, verbosity,
  closure costs, capex                threads
lane existence, rate, distance,
  lead time, capacity, mode
lane baseline-active flag
product weight / value / holding
supply & production capacity
material config switches
  (objective mode, carbon price/cap,
   sourcing policy, SLA enforcement,
   inventory settings, closure &
   contract enforcement, optimization
   mode)

Solver *performance* settings are excluded deliberately: raising a time limit
does not change the optimum, so it must not force a recomputation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from netgravity.schemas.network import CanonicalNetwork, OptimizationConfig

#: Bumped when the fingerprint's own definition changes, so stale entries
#: computed under an older definition can never be mistaken for current ones.
FINGERPRINT_VERSION = "1"

#: Config fields that can change the optimum. Anything not listed is treated as
#: non-material (solver tuning, logging, reporting).
_MATERIAL_CONFIG_FIELDS = (
    "optimization_mode",
    "objective_mode",
    "carbon_price",
    "carbon_cap_kg",
    "carbon_weight",
    "shortage_penalty",
    "allow_shortage",
    "service_metric",
    "enforce_sla",
    "sla_mode",
    "sourcing_policy",
    "max_facilities",
    "budget_capex",
    "minimum_throughput_enabled",
    "enable_inventory",
    "include_cycle_stock",
    "inventory_z_score",
    "days_per_period",
    "enable_carbon_cost",
    "emission_methodology",
    "emission_factor_table",
    "cost_period",
    "enable_closure_cost",
    "enforce_contracts",
)


def _material_facility(f: Any) -> Dict[str, Any]:
    return {
        "id": f.id,
        "role": _v(f.role),
        "status": _v(f.status),
        "baseline_status": _v(f.baseline_status) if f.baseline_status else None,
        "capacity": f.capacity_units_per_period,
        "production_capacity": f.production_capacity_units_per_period,
        "min_throughput": f.min_throughput_per_period,
        "fixed_cost_per_year": f.fixed_cost_per_year,
        "handling_cost_per_unit": f.handling_cost_per_unit,
        "opening_cost": f.opening_cost,
        "closure_cost": f.closure_cost,
        "capex": f.capex,
        "is_closable": f.is_closable,
        "is_mandatory": f.is_mandatory,
        "is_forced_closed": f.is_forced_closed,
        "is_disruption_target": f.is_disruption_target,
        "contract_status": _v(f.contract_status),
        "contract_allows_early_closure": f.contract_allows_early_closure,
        "eligible_product_ids": sorted(f.eligible_product_ids),
        "replenishment_lead_time_days": f.replenishment_lead_time_days,
        # Location matters: moving a facility changes lane geometry.
        "latitude": f.latitude,
        "longitude": f.longitude,
    }


def _material_demand(d: Any) -> Dict[str, Any]:
    return {
        "market_id": d.market_id,
        "product_id": d.product_id,
        "period": d.period,
        "quantity": d.quantity,
        "std_dev": d.std_dev,
        "sla_days": d.sla_days,
        "service_level": d.service_level,
        "priority": d.priority,
    }


def _material_lane(ln: Any) -> Dict[str, Any]:
    return {
        "origin_id": ln.origin_id,
        "destination_id": ln.destination_id,
        "mode": _v(ln.mode),
        "rate_per_unit": ln.rate_per_unit,
        "distance_km": ln.distance_km,
        "network_distance_km": ln.network_distance_km,
        "lead_time_days": ln.lead_time_days,
        "lane_capacity": ln.lane_capacity,
        "emission_factor_override": ln.emission_factor_override,
        "eligible_product_ids": sorted(ln.eligible_product_ids),
        "is_active_baseline": ln.is_active_baseline,
    }


def _material_product(p: Any) -> Dict[str, Any]:
    return {
        "id": p.id,
        "weight_kg": p.weight_kg,
        "volume_m3": p.volume_m3,
        "unit_value": p.unit_value,
        "holding_rate": p.holding_rate,
    }


def _v(value: Any) -> Any:
    """Enum → its value, so the hash is stable across representations."""
    return value.value if hasattr(value, "value") else value


def material_config_view(config: OptimizationConfig) -> Dict[str, Any]:
    """The subset of configuration that can change the optimum."""
    view: Dict[str, Any] = {}
    for field in _MATERIAL_CONFIG_FIELDS:
        view[field] = _v(getattr(config, field, None))
    return view


def compute_material_fingerprint(
    network: CanonicalNetwork,
    config: OptimizationConfig | None = None,
) -> str:
    """
    Hash the material optimization inputs of a network.

    Deterministic and order-independent: records are sorted by identity, so two
    networks describing the same reality fingerprint identically regardless of
    declaration order.

    Args:
        network: The network to fingerprint.
        config:  Configuration to include (defaults to `network.config`).

    Returns:
        A 16-character hex digest, prefixed with the fingerprint version.
    """
    cfg = config or network.config

    facilities: List[Dict[str, Any]] = sorted(
        (_material_facility(f) for f in network.facilities), key=lambda d: d["id"],
    )
    demands: List[Dict[str, Any]] = sorted(
        (_material_demand(d) for d in network.demands),
        key=lambda d: (d["market_id"], d["product_id"], d["period"]),
    )
    lanes: List[Dict[str, Any]] = sorted(
        (_material_lane(ln) for ln in network.lanes),
        key=lambda d: (d["origin_id"], d["destination_id"], str(d["mode"])),
    )
    products: List[Dict[str, Any]] = sorted(
        (_material_product(p) for p in network.products), key=lambda d: d["id"],
    )

    payload = json.dumps(
        {
            "v": FINGERPRINT_VERSION,
            "facilities": facilities,
            "demands": demands,
            "lanes": lanes,
            "products": products,
            "config": material_config_view(cfg),
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"fp{FINGERPRINT_VERSION}_{digest}"


def networks_are_materially_equal(
    a: CanonicalNetwork,
    b: CanonicalNetwork,
    config_a: OptimizationConfig | None = None,
    config_b: OptimizationConfig | None = None,
) -> bool:
    """True when two networks would produce the same optimization result."""
    return (compute_material_fingerprint(a, config_a)
            == compute_material_fingerprint(b, config_b))
