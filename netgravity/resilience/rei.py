"""
NetGravity — Facility Resilience Assessment & Risk Exposure Index (REI)
=======================================================================
Version: 1.0.0

Answers the business question:

    If a facility becomes unavailable, how much additional network cost does
    the business incur after the network optimally reconfigures itself?

Architecture — the engine sits AROUND the MILP, it never replaces it
────────────────────────────────────────────────────────────────────

                        Existing Network
                              │
                        Existing MILP                  (solved once)
                              │
                        Baseline Solution
                              │
                        Business Cost  C_base
                              │
                 ┌────────────┴────────────┐
                 ▼                         ▼
            Facility i                Facility j
            unavailable               unavailable
                 │                         │
            Existing MILP             Existing MILP    (re-optimisation)
                 │                         │
            Business Cost C_i         Business Cost C_j
                 └────────────┬────────────┘
                              ▼
                     PI_i = C_i − C_base
                              ▼
                    REI_i = PI_i / max_j(PI_j)
                              ▼
                 Facility Resilience Registry

There is exactly one optimisation model in NetGravity: `optimization.milp.solve`.
This module calls it; it does not reformulate, approximate, or duplicate it.

What REI means
──────────────
    REI = RELATIVE ECONOMIC EXPOSURE TO FACILITY DISRUPTION.

    The facility whose loss costs the business the most has REI = 1.00.
    Every other facility is scaled against it: 0 ≤ REI < 1.

REI does NOT mean, and must never be presented as: probability of failure,
probability of disruption, a percentage of resilience, an AI confidence score,
a service-level score, or a generic risk score.

Determinism
───────────
Every number here comes from the MILP and from arithmetic on MILP outputs.
No LLM participates in the calculation of PI or REI. The future resilience agent
CONSUMES this registry; it does not produce it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from netgravity.costs.business_cost import (
    BusinessCostError,
    BusinessNetworkCost,
    compute_business_network_cost,
)
from netgravity.optimization.milp import solve as milp_solve
from netgravity.resilience.engine import compute_rerouted_volume
from netgravity.schemas.network import (
    CanonicalNetwork,
    FacilityRecord,
    FacilityStatus,
    NodeRole,
    OptimizationConfig,
)
from netgravity.schemas.resilience import DisruptionConfig, DisruptionType
from netgravity.schemas.results import (
    FacilityResilienceRegistry,
    FacilityResilienceResult,
    OptimizationResult,
    REIStatus,
    RiskClassification,
    SolverStatus,
)

logger = logging.getLogger(__name__)

MARKET_ROLES = {NodeRole.MARKET, NodeRole.CUSTOMER}

# Solver injection point. Kept as a type alias so future execution strategies
# (caching, parallel facility evaluation, async, Azure) can be supplied without
# changing the public interface of this module.
SolveFn = Callable[[CanonicalNetwork, OptimizationConfig, Optional[str]], OptimizationResult]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ResilienceAssessmentError(Exception):
    """Base error for the resilience assessment engine."""


class FacilityNotFoundError(ResilienceAssessmentError):
    """The requested facility does not exist in the network."""


class InvalidDisruptionTargetError(ResilienceAssessmentError):
    """The requested facility cannot be disrupted (e.g. it is a market node)."""


class NoEligibleFacilitiesError(ResilienceAssessmentError):
    """No facility in the network qualifies for assessment."""


class BaselineSolveError(ResilienceAssessmentError):
    """The baseline network could not be solved, so no comparison is possible."""


# ---------------------------------------------------------------------------
# Baseline context (solved ONCE per batch)
# ---------------------------------------------------------------------------

@dataclass
class ResilienceBaseline:
    """
    The undisrupted reference point for a resilience assessment.

    Solved once and reused for every facility in a batch. Carries the effective
    config so that each disrupted solve provably runs under identical
    assumptions — the fair-comparison requirement for REI.
    """
    network:           CanonicalNetwork
    effective_config:  OptimizationConfig
    disruption_config: DisruptionConfig
    result:            OptimizationResult
    business_cost:     BusinessNetworkCost
    total_demand:      float
    served:            float
    carbon:            float
    solve_seconds:     float

    @property
    def cost(self) -> float:
        return self.business_cost.total


def _default_solve(
    network:     CanonicalNetwork,
    config:      OptimizationConfig,
    scenario_id: Optional[str],
) -> OptimizationResult:
    """Default solver hook: the single authoritative MILP."""
    return milp_solve(network=network, config=config, scenario_id=scenario_id)


def _effective_config(
    config:            OptimizationConfig,
    disruption_config: DisruptionConfig,
) -> OptimizationConfig:
    """
    Build the one configuration used for BOTH the baseline and every disrupted
    solve.

    Only `allow_shortage` is overridden, and it is overridden symmetrically. The
    existing ResilienceEngine solves the baseline with the caller's config but
    the disrupted network with shortage enabled; that asymmetry compares two
    different models. REI requires one model.
    """
    return config.model_copy(update={"allow_shortage": disruption_config.allow_shortage})


def _served_and_carbon(
    result:  OptimizationResult,
    network: CanonicalNetwork,
) -> Tuple[float, float]:
    """Total volume delivered to market nodes, and total carbon, from MILP flows."""
    fac_map = {f.id: f for f in network.facilities}
    served = sum(
        fl.flow_units for fl in result.flow_decisions
        if fac_map.get(fl.destination_id) and fac_map[fl.destination_id].role in MARKET_ROLES
    )
    carbon = sum(fl.carbon_kg for fl in result.flow_decisions)
    return round(served, 4), round(carbon, 6)


def compute_baseline(
    network:           CanonicalNetwork,
    config:            Optional[OptimizationConfig] = None,
    disruption_config: Optional[DisruptionConfig] = None,
    solve_fn:          Optional[SolveFn] = None,
) -> ResilienceBaseline:
    """
    Solve the undisrupted network once and compute its business network cost.

    Raises:
        BaselineSolveError: if the baseline is infeasible or the solver errors —
            without a baseline there is nothing to compare against.
    """
    if config is None:
        config = network.config
    if disruption_config is None:
        disruption_config = DisruptionConfig()
    solve_fn = solve_fn or _default_solve

    eff_config = _effective_config(config, disruption_config)

    logger.info(
        "resilience.baseline.started network_id=%s disruption=%s",
        network.network_id, disruption_config.describe(),
    )

    started = time.perf_counter()
    result = solve_fn(network, eff_config, "REI_BASELINE")
    elapsed = round(time.perf_counter() - started, 4)

    if not result.is_solved:
        raise BaselineSolveError(
            f"Baseline network could not be solved (solver status: "
            f"{result.solver.status.value}). A resilience assessment requires a "
            f"feasible baseline to measure incremental cost against."
        )

    try:
        business = compute_business_network_cost(
            result, network, config=eff_config, cost_basis=disruption_config.cost_basis,
        )
    except BusinessCostError as exc:
        raise BaselineSolveError(f"Baseline business cost could not be computed: {exc}") from exc

    total_demand = sum(d.quantity for d in network.demands)
    served, carbon = _served_and_carbon(result, network)

    logger.info(
        "resilience.baseline.business_cost_calculated network_id=%s status=%s "
        "business_cost=%.4f solver_objective=%.4f runtime_s=%.4f",
        network.network_id, result.solver.status.value,
        business.total, business.solver_objective, elapsed,
    )

    return ResilienceBaseline(
        network           = network,
        effective_config  = eff_config,
        disruption_config = disruption_config,
        result            = result,
        business_cost     = business,
        total_demand      = round(total_demand, 4),
        served            = served,
        carbon            = carbon,
        solve_seconds     = elapsed,
    )


# ---------------------------------------------------------------------------
# Facility discovery (never hard-coded)
# ---------------------------------------------------------------------------

def discover_eligible_facilities(
    network:           CanonicalNetwork,
    disruption_config: DisruptionConfig,
    baseline_result:   Optional[OptimizationResult] = None,
) -> List[FacilityRecord]:
    """
    Discover which facilities to assess, from the network model itself.

    Facility identities are never hard-coded anywhere in this engine. Eligibility
    is derived from `network.facilities` plus the configured filters:

      - market/customer nodes are never disruption targets (they are demand, not
        capacity);
      - CLOSED and force-closed facilities are skipped (already unavailable);
      - `eligible_roles`, when set, restricts to those roles;
      - `exclude_facility_ids` removes explicit opt-outs;
      - `only_baseline_open_facilities` (default) restricts to facilities the
        baseline solution actually opens — a facility the optimal baseline does
        not use has no exposure by construction.

    Returns facilities in a deterministic order (network declaration order).
    """
    eligible: List[FacilityRecord] = []
    excluded_ids = set(disruption_config.exclude_facility_ids)

    open_ids: Optional[set] = None
    if disruption_config.only_baseline_open_facilities and baseline_result is not None:
        open_ids = {fd.facility_id for fd in baseline_result.facility_decisions if fd.is_open}

    for fac in network.facilities:
        if fac.role in MARKET_ROLES:
            continue
        if fac.status == FacilityStatus.CLOSED or fac.is_forced_closed:
            continue
        if fac.id in excluded_ids:
            continue
        if disruption_config.eligible_roles is not None and fac.role not in disruption_config.eligible_roles:
            continue
        if open_ids is not None and fac.id not in open_ids:
            continue
        eligible.append(fac)

    return eligible


# ---------------------------------------------------------------------------
# Disruption application
# ---------------------------------------------------------------------------

def apply_facility_disruption(
    network:     CanonicalNetwork,
    facility_id: str,
) -> CanonicalNetwork:
    """
    Return a copy of the network with `facility_id` made unavailable for the
    modelled planning period.

    Unavailability is expressed through mechanisms the existing MILP already
    understands, so no new constraint type is introduced:

        is_forced_closed = True   → constraint (C5b) pins y_i = 0
        capacity         = 0      → constraint (C2) pins outbound flow to 0
        production_cap   = 0      → supply-side limit for plants/suppliers
        is_mandatory     = False  → releases any "must stay open" pin
        is_closable      = True

    Flow conservation (C4) then drives inbound flow to a disrupted intermediate
    node to zero as well.

    Raises:
        FacilityNotFoundError:        unknown facility id.
        InvalidDisruptionTargetError: target is a market/customer node.
    """
    target: Optional[FacilityRecord] = None
    for f in network.facilities:
        if f.id == facility_id:
            target = f
            break

    if target is None:
        known = ", ".join(sorted(f.id for f in network.facilities))
        raise FacilityNotFoundError(
            f"Facility '{facility_id}' not found in network '{network.network_id}'. "
            f"Known facilities: {known}"
        )

    if target.role in MARKET_ROLES:
        raise InvalidDisruptionTargetError(
            f"Facility '{facility_id}' has role {target.role.value} and represents demand, "
            f"not network capacity. Market nodes cannot be disruption targets; use a "
            f"demand-side disruption instead."
        )

    new_facilities = [
        f.model_copy(update={
            "capacity_units_per_period":            0.0,
            "production_capacity_units_per_period": 0.0,
            "min_throughput_per_period":            0.0,
            "is_forced_closed":                     True,
            "is_mandatory":                         False,
            "is_closable":                          True,
        }) if f.id == facility_id else f
        for f in network.facilities
    ]

    return network.model_copy(update={"facilities": new_facilities})


# ---------------------------------------------------------------------------
# REI normalisation (pure arithmetic — no MILP, no LLM)
# ---------------------------------------------------------------------------

def normalize_rei(
    performance_impacts: Sequence[Optional[float]],
) -> Tuple[List[Optional[float]], Optional[float], REIStatus]:
    """
    Normalise Performance Impacts into Risk Exposure Indices.

        REI_i = PI_i / max_j(PI_j)

    Rules, all explicit and safe:

      - max PI > 0  → REI computed for every facility with a PI.
                      The highest-PI facility gets exactly 1.00.
                      A negative PI yields a negative REI: it is RETAINED, not
                      clamped, because a disruption that reduces business cost
                      is an anomaly that must be investigated, not hidden.
      - max PI == 0 → every REI is 0 and the status is
                      NO_RELATIVE_COST_EXPOSURE. No division occurs.
      - max PI < 0  → no facility has positive exposure. Every REI is 0 and the
                      status is NO_RELATIVE_COST_EXPOSURE; dividing by a negative
                      maximum would invert the ranking.
      - PI is None  → REI is None (the facility could not be assessed).

    Args:
        performance_impacts: PI per facility, None where unavailable.

    Returns:
        (reis, max_pi, status) — `reis` aligns positionally with the input.
    """
    known = [pi for pi in performance_impacts if pi is not None]

    if not known:
        return [None for _ in performance_impacts], None, REIStatus.NOT_COMPUTED

    max_pi = max(known)

    if max_pi <= 0.0:
        # Includes the exact-zero case: no relative cost exposure anywhere.
        return (
            [None if pi is None else 0.0 for pi in performance_impacts],
            max_pi,
            REIStatus.NO_RELATIVE_COST_EXPOSURE,
        )

    return (
        [None if pi is None else pi / max_pi for pi in performance_impacts],
        max_pi,
        REIStatus.COMPUTED,
    )


# ---------------------------------------------------------------------------
# Deterministic risk classification
# ---------------------------------------------------------------------------

def classify_risk(
    result:            FacilityResilienceResult,
    disruption_config: DisruptionConfig,
) -> RiskClassification:
    """
    Classify a facility using only explicit deterministic rules.

    No REI band is applied. REI is a relative ranking metric and there is no
    documented business basis for absolute REI thresholds; inventing them would
    manufacture a risk score. Facilities that meet no configured rule are
    reported as NOT_CLASSIFIED, and REI + rank carry the signal.
    """
    rules = disruption_config.risk_rules

    if result.solver_status in (SolverStatus.ERROR, SolverStatus.NO_SOLUTION):
        return RiskClassification.UNKNOWN

    if not result.is_feasible:
        # The network cannot absorb this disruption under the current constraints.
        return RiskClassification.CRITICAL if rules.infeasible_is_critical else RiskClassification.UNKNOWN

    rate = result.unserved_demand_rate
    if rate is not None:
        if rules.unserved_demand_rate_critical is not None and rate >= rules.unserved_demand_rate_critical:
            return RiskClassification.CRITICAL
        if rules.unserved_demand_rate_high is not None and rate >= rules.unserved_demand_rate_high:
            return RiskClassification.HIGH

    if (rules.cost_impact_pct_high is not None
            and result.cost_impact_pct is not None
            and result.cost_impact_pct >= rules.cost_impact_pct_high):
        return RiskClassification.HIGH

    return RiskClassification.NOT_CLASSIFIED


# ---------------------------------------------------------------------------
# Service diagnostic for infeasible disruptions
# ---------------------------------------------------------------------------

def _service_diagnostic(
    disrupted_network: CanonicalNetwork,
    baseline:          ResilienceBaseline,
    disruption_config: DisruptionConfig,
    facility_id:       str,
    solve_fn:          SolveFn,
) -> Tuple[Dict[str, object], bool, float]:
    """
    Quantify the service damage of a disruption that is INFEASIBLE under the
    primary like-for-like basis.

    Re-solves the disrupted network ONCE with shortage enabled. This exists so a
    CRITICAL facility still reports how much demand it strands — the registry
    would otherwise carry a bare "infeasible" with no measure of severity, and
    could not rank two critical facilities against each other operationally.

    This is strictly a diagnostic. It returns ONLY service and carbon fields.
    No cost, Performance Impact, Cost Impact or REI is derived from it, so the
    artificial shortage penalty cannot reach the cost-based ranking.

    Returns:
        (service_fields, applied, seconds)
    """
    diag_config = baseline.effective_config.model_copy(update={"allow_shortage": True})

    started = time.perf_counter()
    diag_result = solve_fn(
        disrupted_network, diag_config, f"REI_SERVICE_DIAG_{facility_id}",
    )
    seconds = round(time.perf_counter() - started, 4)

    if not diag_result.is_solved:
        logger.warning(
            "resilience.facility.service_diagnostic_failed facility_id=%s status=%s",
            facility_id, diag_result.solver.status.value,
        )
        return {}, False, seconds

    served, carbon = _served_and_carbon(diag_result, disrupted_network)
    unserved = round(max(0.0, baseline.total_demand - served), 4)

    # Shortage penalty is quantified and explicitly labelled as excluded.
    shortage_penalty = 0.0
    try:
        diag_business = compute_business_network_cost(
            diag_result, disrupted_network,
            config=diag_config, cost_basis=disruption_config.cost_basis,
        )
        shortage_penalty = diag_business.shortage_penalty_cost
    except BusinessCostError:
        pass

    fields: Dict[str, object] = {
        "disrupted_served":          served,
        "unserved_demand":           unserved,
        "unserved_demand_rate":      (
            round(unserved / baseline.total_demand, 6) if baseline.total_demand > 0 else None
        ),
        "service_loss":              (
            round((baseline.served - served) / baseline.served, 6) if baseline.served > 0 else None
        ),
        "rerouted_volume":           round(compute_rerouted_volume(baseline.result, diag_result), 4),
        "disrupted_carbon":          carbon,
        "carbon_delta":              round(carbon - baseline.carbon, 6),
        "excluded_shortage_penalty": shortage_penalty,
    }

    logger.info(
        "resilience.facility.service_diagnostic_completed facility_id=%s unserved=%.4f "
        "runtime_s=%.4f",
        facility_id, unserved, seconds,
    )
    return fields, True, seconds


# ---------------------------------------------------------------------------
# Facility-level assessment
# ---------------------------------------------------------------------------

def assess_facility_resilience(
    network:           CanonicalNetwork,
    config:            Optional[OptimizationConfig] = None,
    facility_id:       str = "",
    disruption_config: Optional[DisruptionConfig] = None,
    *,
    baseline:          Optional[ResilienceBaseline] = None,
    solve_fn:          Optional[SolveFn] = None,
) -> FacilityResilienceResult:
    """
    Assess the economic exposure of ONE facility to disruption.

        PI_i = C_business_i − C_business_base
        CI_i = PI_i / C_business_base × 100

    REI is deliberately NOT set here: it is relative to the other facilities in
    the batch and can only be assigned once every PI is known. Call
    `assess_network_resilience` for a ranked registry with REI populated.

    Args:
        network:           The undisrupted canonical network.
        config:            Optimization config (uses network.config if None).
        facility_id:       Facility to disrupt. Must exist and must not be a market.
        disruption_config: Disruption assumptions (defaults to DisruptionConfig()).
        baseline:          Precomputed baseline. Pass this when assessing several
                           facilities so the baseline is solved once, not N times.
        solve_fn:          Solver hook; defaults to the authoritative MILP.

    Returns:
        FacilityResilienceResult. On an infeasible disruption, business cost,
        PI and CI are None and the result is flagged CRITICAL — no fabricated
        cost is produced.

    Raises:
        FacilityNotFoundError, InvalidDisruptionTargetError, BaselineSolveError.
    """
    if config is None:
        config = network.config
    if disruption_config is None:
        disruption_config = DisruptionConfig()
    solve_fn = solve_fn or _default_solve

    if not facility_id:
        raise FacilityNotFoundError("facility_id is required and must be a non-empty string.")

    if disruption_config.disruption_type != DisruptionType.FACILITY_FAILURE:
        raise ResilienceAssessmentError(
            f"Unsupported disruption type for facility assessment: "
            f"{disruption_config.disruption_type.value}"
        )

    if baseline is None:
        baseline = compute_baseline(network, config, disruption_config, solve_fn=solve_fn)

    # Disrupt first so an unknown/invalid facility fails loudly before solving.
    disrupted_network = apply_facility_disruption(network, facility_id)
    target = disrupted_network.get_facility(facility_id)

    diagnostics: List[str] = []

    logger.info(
        "resilience.facility.started facility_id=%s disruption=%s",
        facility_id, disruption_config.describe(),
    )

    started = time.perf_counter()
    disrupted_result = solve_fn(
        disrupted_network, baseline.effective_config, f"REI_DISRUPT_{facility_id}",
    )
    elapsed = round(time.perf_counter() - started, 4)

    common = dict(
        facility_id               = facility_id,
        facility_name             = target.name,
        facility_role             = target.role.value,
        disruption_type           = disruption_config.disruption_type.value,
        disruption_period         = disruption_config.disruption_period.value,
        baseline_business_cost    = baseline.cost,
        baseline_served           = baseline.served,
        baseline_carbon           = baseline.carbon,
        baseline_solver_objective = baseline.business_cost.solver_objective,
        solve_seconds             = elapsed,
    )

    # ---- Infeasible / unsolved disruption: no fabricated cost --------------
    if not disrupted_result.is_solved:
        diagnostics.append(
            f"Disruption of '{facility_id}' left the network without a feasible solution "
            f"(solver status: {disrupted_result.solver.status.value}). The network cannot "
            f"absorb this disruption under the current constraints. Performance Impact and "
            f"REI are undefined and reported as None rather than estimated."
        )

        service_fields: Dict[str, object] = {}
        diagnostic_applied = False

        if disruption_config.service_diagnostic_on_infeasible and not disruption_config.allow_shortage:
            # DIAGNOSTIC pass only: re-solve with shortage enabled purely to
            # quantify how much service is lost. Cost fields stay None, so the
            # artificial penalty can never reach PI, CI or REI.
            service_fields, diagnostic_applied, extra_seconds = _service_diagnostic(
                disrupted_network, baseline, disruption_config, facility_id, solve_fn,
            )
            if diagnostic_applied:
                elapsed = round(elapsed + extra_seconds, 4)
                common["solve_seconds"] = elapsed
                diagnostics.append(
                    f"Service diagnostic: re-solved '{facility_id}' with shortage enabled to "
                    f"quantify the damage. {service_fields.get('unserved_demand', 0.0):,.2f} units "
                    f"({(service_fields.get('unserved_demand_rate') or 0.0) * 100:.2f}% of demand) "
                    f"cannot be served. These service figures are diagnostic only — cost, "
                    f"Performance Impact and REI remain undefined for this facility."
                )

        res = FacilityResilienceResult(
            solver_status              = disrupted_result.solver.status,
            is_feasible                = False,
            rei_status                 = REIStatus.NOT_COMPUTED,
            service_diagnostic_applied = diagnostic_applied,
            diagnostics                = diagnostics,
            **{**common, **service_fields},
        )
        res.risk_classification = classify_risk(res, disruption_config)
        logger.warning(
            "resilience.facility.completed facility_id=%s status=%s feasible=False "
            "service_diagnostic=%s unserved_rate=%s risk=%s runtime_s=%.4f",
            facility_id, disrupted_result.solver.status.value, diagnostic_applied,
            f"{res.unserved_demand_rate:.6f}" if res.unserved_demand_rate is not None else "None",
            res.risk_classification.value, elapsed,
        )
        return res

    # ---- Business network cost of the re-optimised network -----------------
    try:
        disrupted_business = compute_business_network_cost(
            disrupted_result, disrupted_network,
            config=baseline.effective_config,
            cost_basis=disruption_config.cost_basis,
        )
    except BusinessCostError as exc:
        diagnostics.append(f"Business cost reconciliation failed: {exc}")
        res = FacilityResilienceResult(
            solver_status = disrupted_result.solver.status,
            is_feasible   = False,
            rei_status    = REIStatus.NOT_COMPUTED,
            diagnostics   = diagnostics,
            **common,
        )
        res.risk_classification = RiskClassification.UNKNOWN
        logger.error(
            "resilience.facility.business_cost_failed facility_id=%s error=%s", facility_id, exc,
        )
        return res

    diagnostics.extend(disrupted_business.notes)

    logger.info(
        "resilience.facility.business_cost_calculated facility_id=%s business_cost=%.4f "
        "solver_objective=%.4f excluded_shortage_penalty=%.4f",
        facility_id, disrupted_business.total, disrupted_business.solver_objective,
        disrupted_business.shortage_penalty_cost,
    )

    # ---- Operational diagnostics (retained, never folded into cost) --------
    served, carbon = _served_and_carbon(disrupted_result, disrupted_network)
    unserved = round(max(0.0, baseline.total_demand - served), 4)
    unserved_rate = round(unserved / baseline.total_demand, 6) if baseline.total_demand > 0 else None
    service_loss = (
        round((baseline.served - served) / baseline.served, 6) if baseline.served > 0 else None
    )
    rerouted = round(compute_rerouted_volume(baseline.result, disrupted_result), 4)

    # ---- Performance Impact & Cost Impact ----------------------------------
    performance_impact = round(disrupted_business.total - baseline.cost, 4)

    if performance_impact < 0.0:
        # Retained, never silently clamped: a disruption that lowers business
        # cost is an anomaly that must be investigated, not hidden.
        if unserved > 0.0:
            # The dominant explanation, and a genuine comparability caveat: the
            # disrupted network serves LESS volume, so part of its lower business
            # cost is simply cost avoided by not serving demand. Business cost is
            # not like-for-like across solutions with different served volumes.
            msg = (
                f"NEGATIVE PERFORMANCE IMPACT with unserved demand: disrupting "
                f"'{facility_id}' produced a business network cost "
                f"{abs(performance_impact):,.4f} LOWER than baseline "
                f"({disrupted_business.total:,.4f} vs {baseline.cost:,.4f}) while leaving "
                f"{unserved:,.2f} units ({(unserved_rate or 0.0) * 100:.2f}% of demand) "
                f"unserved. The comparison is NOT like-for-like: the disrupted network is "
                f"serving less volume, so part of the apparent saving is cost avoided by "
                f"not serving demand rather than a genuine efficiency gain. Treat this "
                f"facility's PI and REI as not directly comparable to fully-served "
                f"facilities; read unserved_demand_rate and service_loss as the material "
                f"exposure signal instead. To force a like-for-like cost comparison, "
                f"re-run with allow_shortage=False so an unabsorbable disruption reports "
                f"as INFEASIBLE."
            )
        else:
            msg = (
                f"NEGATIVE PERFORMANCE IMPACT: disrupting '{facility_id}' produced a business "
                f"network cost {abs(performance_impact):,.4f} LOWER than baseline "
                f"({disrupted_business.total:,.4f} vs {baseline.cost:,.4f}) with all demand "
                f"still served. The raw value is retained. Investigate whether this reflects "
                f"genuine model behaviour (e.g. an open facility whose fixed cost exceeded its "
                f"routing benefit, meaning the baseline network itself is suboptimal), a cost "
                f"configuration issue, or an invalid comparison."
            )
        diagnostics.append(msg)
        logger.warning(
            "resilience.facility.negative_pi facility_id=%s pi=%.4f unserved=%.4f",
            facility_id, performance_impact, unserved,
        )

    if baseline.cost > 0.0:
        cost_impact_pct = round(performance_impact / baseline.cost * 100.0, 6)
    else:
        cost_impact_pct = None
        diagnostics.append(
            "cost_impact_pct is undefined: baseline business network cost is zero, so a "
            "percentage increase cannot be expressed."
        )

    logger.info(
        "resilience.facility.performance_impact_calculated facility_id=%s pi=%.4f cost_impact_pct=%s",
        facility_id, performance_impact,
        f"{cost_impact_pct:.4f}" if cost_impact_pct is not None else "None",
    )

    res = FacilityResilienceResult(
        disrupted_business_cost    = disrupted_business.total,
        performance_impact         = performance_impact,
        cost_impact_pct            = cost_impact_pct,
        rei_status                 = REIStatus.NOT_COMPUTED,   # assigned by the registry
        disrupted_served           = served,
        service_loss               = service_loss,
        unserved_demand            = unserved,
        unserved_demand_rate       = unserved_rate,
        rerouted_volume            = rerouted,
        disrupted_carbon           = carbon,
        carbon_delta               = round(carbon - baseline.carbon, 6),
        solver_status              = disrupted_result.solver.status,
        is_feasible                = True,
        disrupted_solver_objective = disrupted_business.solver_objective,
        excluded_shortage_penalty  = disrupted_business.shortage_penalty_cost,
        diagnostics                = diagnostics,
        **common,
    )
    res.risk_classification = classify_risk(res, disruption_config)

    logger.info(
        "resilience.facility.completed facility_id=%s status=%s pi=%.4f unserved_rate=%s "
        "risk=%s runtime_s=%.4f",
        facility_id, disrupted_result.solver.status.value, performance_impact,
        f"{unserved_rate:.6f}" if unserved_rate is not None else "None",
        res.risk_classification.value, elapsed,
    )
    return res


# ---------------------------------------------------------------------------
# Network-level assessment (the registry)
# ---------------------------------------------------------------------------

def assess_network_resilience(
    network:           CanonicalNetwork,
    config:            Optional[OptimizationConfig] = None,
    disruption_config: Optional[DisruptionConfig] = None,
    *,
    solve_fn:          Optional[SolveFn] = None,
) -> FacilityResilienceRegistry:
    """
    Assess every eligible facility and return a ranked Facility Resilience Registry.

    Executes 1 + N MILP solves: the baseline once, then one re-optimisation per
    eligible facility. The baseline is never re-solved inside the loop.

    Every facility in the returned registry was evaluated under the SAME
    disruption type, disruption period, demand, cost parameters, service
    constraints, capacity assumptions and model configuration — the condition
    under which REI is meaningful.

    Args:
        network:           The undisrupted canonical network.
        config:            Optimization config (uses network.config if None).
        disruption_config: One shared set of disruption assumptions.
        solve_fn:          Solver hook. The default is the authoritative MILP;
                           supplying an alternative is the extension point for
                           caching, parallel evaluation, async execution or
                           remote (Azure) execution, with no interface change.

    Returns:
        FacilityResilienceRegistry, ranked by descending Performance Impact.

    Raises:
        BaselineSolveError:        the baseline is infeasible.
        NoEligibleFacilitiesError: no facility qualifies for assessment.
    """
    if config is None:
        config = network.config
    if disruption_config is None:
        disruption_config = DisruptionConfig()
    solve_fn = solve_fn or _default_solve

    batch_started = time.perf_counter()
    warnings_list: List[str] = []

    baseline = compute_baseline(network, config, disruption_config, solve_fn=solve_fn)
    warnings_list.extend(baseline.business_cost.notes)

    facilities = discover_eligible_facilities(network, disruption_config, baseline.result)
    if not facilities:
        raise NoEligibleFacilitiesError(
            f"No eligible facilities found in network '{network.network_id}' under the "
            f"configured filters (eligible_roles={disruption_config.eligible_roles}, "
            f"only_baseline_open_facilities={disruption_config.only_baseline_open_facilities}, "
            f"exclude_facility_ids={disruption_config.exclude_facility_ids}). "
            f"Nothing can be assessed."
        )

    logger.info(
        "resilience.registry.assessment_started network_id=%s n_facilities=%d disruption=%s",
        network.network_id, len(facilities), disruption_config.describe(),
    )

    results: List[FacilityResilienceResult] = []
    for fac in facilities:
        try:
            results.append(assess_facility_resilience(
                network, config, fac.id, disruption_config,
                baseline=baseline, solve_fn=solve_fn,
            ))
        except ResilienceAssessmentError as exc:
            # Never silently swallowed: recorded as an explicit UNKNOWN row.
            logger.error("resilience.facility.failed facility_id=%s error=%s", fac.id, exc)
            warnings_list.append(f"Facility '{fac.id}' could not be assessed: {exc}")
            results.append(FacilityResilienceResult(
                facility_id            = fac.id,
                facility_name          = fac.name,
                facility_role          = fac.role.value,
                disruption_type        = disruption_config.disruption_type.value,
                disruption_period      = disruption_config.disruption_period.value,
                baseline_business_cost = baseline.cost,
                solver_status          = SolverStatus.ERROR,
                is_feasible            = False,
                rei_status             = REIStatus.NOT_COMPUTED,
                risk_classification    = RiskClassification.UNKNOWN,
                diagnostics            = [f"Assessment error: {exc}"],
            ))

    # ---- REI normalisation across the batch --------------------------------
    reis, max_pi, rei_status = normalize_rei([r.performance_impact for r in results])
    for res, rei in zip(results, reis):
        res.rei = None if rei is None else round(rei, 6)
        res.rei_status = rei_status if res.performance_impact is not None else REIStatus.NOT_COMPUTED

    logger.info(
        "resilience.registry.rei_calculated network_id=%s max_pi=%s status=%s",
        network.network_id,
        f"{max_pi:.4f}" if max_pi is not None else "None",
        rei_status.value,
    )

    if rei_status == REIStatus.NO_RELATIVE_COST_EXPOSURE:
        warnings_list.append(
            f"No relative cost exposure: the maximum Performance Impact across all assessed "
            f"facilities is {max_pi:,.4f} (not positive), so every REI is 0. No facility "
            f"increases business network cost when disrupted under these assumptions."
        )

    # ---- Deterministic ranking ---------------------------------------------
    # Descending PI; unrankable rows (PI = None) last; facility_id breaks ties
    # so repeated runs on identical inputs produce an identical ordering.
    results.sort(key=lambda r: (
        r.performance_impact is None,
        -(r.performance_impact or 0.0),
        r.facility_id,
    ))
    for idx, res in enumerate(results, start=1):
        res.rank = idx if res.performance_impact is not None else None

    n_infeasible = sum(1 for r in results if not r.is_feasible)
    n_diagnostic = sum(1 for r in results if r.service_diagnostic_applied)
    total_seconds = round(time.perf_counter() - batch_started, 4)

    logger.info(
        "resilience.registry.ranking_completed network_id=%s n_assessed=%d n_infeasible=%d "
        "n_diagnostic_solves=%d total_milp_solves=%d baseline_solve_s=%.4f total_s=%.4f",
        network.network_id, len(results), n_infeasible, n_diagnostic,
        1 + len(results) + n_diagnostic, baseline.solve_seconds, total_seconds,
    )

    return FacilityResilienceRegistry(
        network_id                = network.network_id,
        data_version              = network.data_version,
        disruption_type           = disruption_config.disruption_type.value,
        disruption_period         = disruption_config.disruption_period.value,
        disruption_summary        = disruption_config.describe(),
        cost_basis_components     = list(baseline.business_cost.included_components),
        excluded_components       = list(baseline.business_cost.excluded_components.keys()),
        baseline_business_cost    = baseline.cost,
        baseline_solver_objective = baseline.business_cost.solver_objective,
        baseline_served           = baseline.served,
        baseline_carbon           = baseline.carbon,
        baseline_solver_status    = baseline.result.solver.status,
        max_performance_impact    = None if max_pi is None else round(max_pi, 4),
        rei_status                = rei_status,
        results                   = results,
        n_facilities_assessed     = len(results),
        n_infeasible              = n_infeasible,
        n_diagnostic_solves       = n_diagnostic,
        baseline_solve_seconds    = baseline.solve_seconds,
        total_assessment_seconds  = total_seconds,
        warnings                  = warnings_list,
        generated_at              = datetime.now().isoformat(),
    )
