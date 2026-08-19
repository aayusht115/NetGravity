"""
NetGravity — Sensitivity Analysis Engine
==========================================
Systematic parameter variation to measure model sensitivity.

Supported analysis types:
  1. One-way sweep: vary one parameter over a range
  2. Two-way sweep: vary two parameters over a grid (cross-product)
  3. Tornado: rank parameters by impact on objective

Parameters supported:
  - transport_cost (multiplier on all lane rates)
  - demand (multiplier on all demand)
  - capacity (multiplier on all facility capacities)
  - carbon_factor (multiplier on emission factors)
  - service_target (SLA days change)
  - fixed_cost (multiplier on facility fixed costs)

Output: List of SensitivityResult objects, each dashboard-ready.

Faculty guidance #32: Sensitivity is a first-class capability.
Faculty guidance #33: Output must support tornado-ready format.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from netgravity.schemas.network import CanonicalNetwork, OptimizationConfig
from netgravity.schemas.results import OptimizationResult, SensitivityPoint, SensitivityResult
from netgravity.optimization.milp import solve as milp_solve
from netgravity.metrics.kpis import compute_kpis


# ---------------------------------------------------------------------------
# Supported sensitivity parameters
# ---------------------------------------------------------------------------

SENSITIVITY_PARAMETERS = {
    "transport_cost":  "Multiplier on all lane transport rates",
    "demand":          "Multiplier on all demand quantities",
    "capacity":        "Multiplier on all facility capacities",
    "carbon_factor":   "Multiplier on emission factors",
    "fixed_cost":      "Multiplier on all facility fixed costs",
    "handling_cost":   "Multiplier on all facility handling costs",
    "distance":        "Multiplier on all lane distances (km)",
    "service_target":  "Multiplier on all demand SLA days (e.g. 0.8 = 20% tighter SLA)",
}


# ---------------------------------------------------------------------------
# Sensitivity engine
# ---------------------------------------------------------------------------

class SensitivityEngine:
    """
    Runs systematic sensitivity sweeps on a CanonicalNetwork.
    """

    def one_way_sweep(
        self,
        network:    CanonicalNetwork,
        parameter:  str,
        values:     List[float],
        config:     Optional[OptimizationConfig] = None,
    ) -> SensitivityResult:
        """
        Sweep one parameter through the given values and record objective.

        Args:
            network:    Base canonical network
            parameter:  Parameter to sweep (see SENSITIVITY_PARAMETERS)
            values:     List of parameter values to test
            config:     Optimization config

        Returns:
            SensitivityResult with all sweep points
        """
        if parameter not in SENSITIVITY_PARAMETERS:
            raise ValueError(
                f"Unknown sensitivity parameter '{parameter}'. "
                f"Choose from: {list(SENSITIVITY_PARAMETERS.keys())}"
            )
        if config is None:
            config = network.config

        # Solve baseline (parameter = 1.0 or its natural value)
        baseline_result = milp_solve(network=network, config=config, scenario_id="BASELINE")
        baseline_kpis   = compute_kpis(baseline_result, network)
        baseline_obj    = baseline_result.solver.objective_value or 0.0

        points: List[SensitivityPoint] = []

        for val in values:
            # Build modified network for this parameter value
            mod_network, mod_config = self._apply_parameter(
                network=network, config=config, parameter=parameter, value=val
            )

            result = milp_solve(network=mod_network, config=mod_config, scenario_id=f"sens_{parameter}_{val:.4g}")
            kpis   = compute_kpis(result, mod_network)

            obj_val = result.solver.objective_value or 0.0

            points.append(SensitivityPoint(
                parameter         = parameter,
                parameter_value   = val,
                objective_value   = round(obj_val, 4),
                n_facilities      = kpis.n_facilities_open,
                total_carbon_kg   = kpis.total_carbon_kg,
                avg_distance_km   = kpis.avg_distance_km,
                demand_fill_rate  = kpis.demand_fill_rate,
            ))

        obj_vals    = [p.objective_value for p in points]
        obj_at_min  = min(obj_vals) if obj_vals else 0.0
        obj_at_max  = max(obj_vals) if obj_vals else 0.0
        obj_range   = obj_at_max - obj_at_min
        sens_pct    = (obj_range / baseline_obj * 100) if baseline_obj != 0 else 0.0

        return SensitivityResult(
            parameter       = parameter,
            baseline_value  = 1.0,
            points          = points,
            obj_at_min      = round(obj_at_min, 4),
            obj_at_max      = round(obj_at_max, 4),
            obj_range       = round(obj_range, 4),
            sensitivity_pct = round(sens_pct, 2),
            baseline_obj    = round(baseline_obj, 4),
        )

    def two_way_sweep(
        self,
        network:      CanonicalNetwork,
        parameter_1:  str,
        values_1:     List[float],
        parameter_2:  str,
        values_2:     List[float],
        config:       Optional[OptimizationConfig] = None,
    ) -> List[Dict]:
        """
        Two-way sensitivity grid.

        Returns a list of dicts suitable for a heatmap:
          {parameter_1: val1, parameter_2: val2, objective_value: z}
        """
        if config is None:
            config = network.config

        grid = []
        for v1 in values_1:
            for v2 in values_2:
                # Apply both parameters
                mod_network_1, mod_config_1 = self._apply_parameter(
                    network=network, config=config, parameter=parameter_1, value=v1
                )
                mod_network_2, mod_config_2 = self._apply_parameter(
                    network=mod_network_1, config=mod_config_1, parameter=parameter_2, value=v2
                )
                result = milp_solve(
                    network=mod_network_2, config=mod_config_2,
                    scenario_id=f"sens2_{parameter_1}_{v1:.3g}_{parameter_2}_{v2:.3g}"
                )
                grid.append({
                    parameter_1:    v1,
                    parameter_2:    v2,
                    "objective":    round(result.solver.objective_value or 0.0, 4),
                    "status":       result.solver.status.value,
                    "n_facilities": len(result.get_open_facilities()),
                })

        return grid

    def tornado_analysis(
        self,
        network:       CanonicalNetwork,
        parameters:    Optional[List[str]] = None,
        variation_pct: float = 0.20,   # ±20% variation
        config:        Optional[OptimizationConfig] = None,
    ) -> List[Dict]:
        """
        Tornado analysis: rank parameters by objective impact.

        For each parameter, solve at (1-variation) and (1+variation),
        record the objective range. Sort by range descending.

        Args:
            parameters:    Parameters to include (default: all)
            variation_pct: Fractional variation (e.g., 0.20 = ±20%)

        Returns:
            List of dicts sorted by impact (largest first), tornado-ready
        """
        if parameters is None:
            parameters = list(SENSITIVITY_PARAMETERS.keys())
        if config is None:
            config = network.config

        baseline_result = milp_solve(network=network, config=config, scenario_id="BASELINE")
        baseline_obj    = baseline_result.solver.objective_value or 0.0

        tornado = []
        for param in parameters:
            lo = 1.0 - variation_pct
            hi = 1.0 + variation_pct

            # Low end
            mod_net_lo, mod_cfg_lo = self._apply_parameter(network, config, param, lo)
            res_lo = milp_solve(mod_net_lo, mod_cfg_lo, scenario_id=f"tornado_{param}_lo")
            obj_lo = res_lo.solver.objective_value or baseline_obj

            # High end
            mod_net_hi, mod_cfg_hi = self._apply_parameter(network, config, param, hi)
            res_hi = milp_solve(mod_net_hi, mod_cfg_hi, scenario_id=f"tornado_{param}_hi")
            obj_hi = res_hi.solver.objective_value or baseline_obj

            impact = abs(obj_hi - obj_lo)
            tornado.append({
                "parameter":       param,
                "description":     SENSITIVITY_PARAMETERS[param],
                "value_low":       round(lo, 3),
                "value_high":      round(hi, 3),
                "obj_at_low":      round(obj_lo, 4),
                "obj_at_high":     round(obj_hi, 4),
                "obj_range":       round(impact, 4),
                "pct_of_baseline": round(impact / baseline_obj * 100, 2) if baseline_obj != 0 else 0,
                "baseline_obj":    round(baseline_obj, 4),
            })

        tornado.sort(key=lambda t: t["obj_range"], reverse=True)
        return tornado

    # ------------------------------------------------------------------
    # Internal: Apply a single parameter override
    # ------------------------------------------------------------------

    def _apply_parameter(
        self,
        network:   CanonicalNetwork,
        config:    OptimizationConfig,
        parameter: str,
        value:     float,
    ) -> Tuple[CanonicalNetwork, OptimizationConfig]:
        """Apply a parameter multiplier and return modified (network, config)."""

        if parameter == "transport_cost":
            new_lanes = [
                ln.model_copy(update={"rate_per_unit": ln.rate_per_unit * value})
                for ln in network.lanes
            ]
            return network.model_copy(update={"lanes": new_lanes}), config

        elif parameter == "demand":
            new_demands = [
                d.model_copy(update={"quantity": max(0.0, d.quantity * value)})
                for d in network.demands
            ]
            return network.model_copy(update={"demands": new_demands}), config

        elif parameter == "capacity":
            new_facilities = []
            for f in network.facilities:
                if f.role.value != "MARKET" and f.capacity_units_per_period < 1e11:
                    new_facilities.append(
                        f.model_copy(update={"capacity_units_per_period":
                                             max(0.0, f.capacity_units_per_period * value)})
                    )
                else:
                    new_facilities.append(f)
            return network.model_copy(update={"facilities": new_facilities}), config

        elif parameter == "carbon_factor":
            # Update via config_overrides on emission factors
            # For now: multiply all lane emission factor overrides
            new_lanes = []
            for ln in network.lanes:
                ef = ln.emission_factor_override
                if ef is not None:
                    new_lanes.append(ln.model_copy(update={"emission_factor_override": ef * value}))
                else:
                    from netgravity.config.defaults import EMISSION_FACTORS_KG_CO2_PER_TONNE_KM
                    mode_key = ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode)
                    base_ef  = EMISSION_FACTORS_KG_CO2_PER_TONNE_KM.get(mode_key, 0.062)
                    new_lanes.append(ln.model_copy(update={"emission_factor_override": base_ef * value}))
            return network.model_copy(update={"lanes": new_lanes}), config

        elif parameter == "fixed_cost":
            new_facilities = []
            for f in network.facilities:
                new_facilities.append(
                    f.model_copy(update={"fixed_cost_per_year":
                                         max(0.0, f.fixed_cost_per_year * value)})
                )
            return network.model_copy(update={"facilities": new_facilities}), config

        elif parameter == "handling_cost":
            new_facilities = []
            for f in network.facilities:
                new_facilities.append(
                    f.model_copy(update={"handling_cost_per_unit":
                                         max(0.0, f.handling_cost_per_unit * value)})
                )
            return network.model_copy(update={"facilities": new_facilities}), config

        elif parameter == "distance":
            new_lanes = [
                ln.model_copy(update={"distance_km": max(0.0, ln.distance_km * value)})
                for ln in network.lanes
            ]
            return network.model_copy(update={"lanes": new_lanes}), config

        elif parameter == "service_target":
            new_demands = [
                d.model_copy(update={"sla_days": max(0.0, d.sla_days * value)})
                if d.sla_days is not None else d
                for d in network.demands
            ]
            return network.model_copy(update={"demands": new_demands}), config

        else:
            raise ValueError(f"Unsupported parameter: '{parameter}'")
