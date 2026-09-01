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
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from netgravity.costs.business_cost import (
    BusinessCostError,
    BusinessNetworkCost,
    compute_business_network_cost,
)
from netgravity.optimization.milp import solve as milp_solve
from netgravity.resilience.engine import compute_rerouted_volume, shortage_config
from netgravity.resilience.fingerprint import compute_material_fingerprint
from netgravity.schemas.network import (
    CanonicalNetwork,
    FacilityRecord,
    FacilityStatus,
    NodeRole,
    OptimizationConfig,
    OptimizationMode,
)
from netgravity.schemas.resilience import DisruptionConfig, DisruptionType
from netgravity.schemas.results import (
    CalculationStatus,
    FacilityResilienceRegistry,
    FacilityResilienceResult,
    OptimizationResult,
    REIBatchStatus,
    REIStatus,
    RiskClassification,
    SolverStatus,
)

logger = logging.getLogger(__name__)

MARKET_ROLES = {NodeRole.MARKET, NodeRole.CUSTOMER}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

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

    # Identity of the data and maths this baseline was produced from. Carried
    # onto every derived row so a REI value can always be tied back to the exact
    # snapshot and model version that produced it.
    snapshot_id:          Optional[str] = None
    material_fingerprint: Optional[str] = None
    model_version:        Optional[str] = None

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

    Two settings are overridden, both SYMMETRICALLY so the baseline and every
    disrupted run share exactly one model:

      allow_shortage      per the disruption config. The existing
                          ResilienceEngine solves the baseline with the caller's
                          config but the disrupted network with shortage
                          enabled; that asymmetry compares two different models.
                          REI requires one model.

      optimization_mode   forced to DISRUPTION_RESILIENCE_OPTIMIZATION. A
                          footprint-locking mode (ACTUAL_AS_IS or
                          CURRENT_FOOTPRINT) pins existing facilities open,
                          which directly contradicts disrupting one — every
                          disruption would return infeasible for the wrong
                          reason. Disruption mode honours facility flags as
                          supplied and applies the disruption-target exemptions
                          from closure cost and contractual pins.
    """
    return config.model_copy(update={
        "allow_shortage":    disruption_config.allow_shortage,
        "optimization_mode": OptimizationMode.DISRUPTION_RESILIENCE_OPTIMIZATION,
    })


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
    *,
    snapshot_id:       Optional[str] = None,
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

    if not result.is_solved and getattr(
            eff_config, "relax_to_shortage_when_infeasible", False) \
            and not eff_config.allow_shortage:
        # Measure against the best achievable plan when no fully-served one
        # exists.
        #
        # A real client network frequently cannot serve all its demand within
        # its own service levels, and this baseline is solved with the STRICT
        # config — so `assess_network_resilience()` raised on every such
        # network and `resilience.assess` failed with ENGINE_FAILURE on every
        # orchestrator run. The disruption solves below already run with
        # `allow_shortage=True` (see `resilience/engine.py`), so the baseline
        # was the only part of the assessment refusing to permit shortage: it
        # was being held to a stricter standard than the disruptions it exists
        # to be compared against.
        #
        # Same relaxation as the optimizer's, and only when the caller asked
        # for it. `unserved_demand` on the baseline records the shortfall, so
        # a comparison against it is not mistaken for a fully-served reference.
        logger.info(
            "resilience.baseline.relaxed network_id=%s reason=strict_infeasible",
            network.network_id,
        )
        # `shortage_config` also sets an ABSOLUTE optimality gap. Permitting
        # shortage inflates the objective by `shortage_penalty x unserved`, at
        # which point the 0.1% relative gap is worth millions of rupees of real
        # cost — and every REI figure is a DIFFERENCE of two such costs, so the
        # difference can be swamped by how far each solve stopped short. That is
        # what produces a negative performance impact for a facility whose
        # closure cannot possibly save money.
        eff_config = shortage_config(network, eff_config)
        result = solve_fn(network, eff_config, "REI_BASELINE_RELAXED")

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

    # Fingerprint the CALLER's config, not the disruption-adjusted effective
    # one. The disruption overrides are deterministic given the disruption
    # config, which the cache key already captures via `disruption_signature`;
    # fingerprinting the effective config here would make the recorded value
    # disagree with the key the service looked up under.
    fingerprint = compute_material_fingerprint(network, config)

    logger.info(
        "resilience.baseline.business_cost_calculated network_id=%s status=%s "
        "business_cost=%.4f solver_objective=%.4f runtime_s=%.4f fingerprint=%s",
        network.network_id, result.solver.status.value,
        business.total, business.solver_objective, elapsed, fingerprint,
    )

    return ResilienceBaseline(
        network              = network,
        effective_config     = eff_config,
        disruption_config    = disruption_config,
        result               = result,
        business_cost        = business,
        total_demand         = round(total_demand, 4),
        served               = served,
        carbon               = carbon,
        solve_seconds        = elapsed,
        snapshot_id          = snapshot_id or network.data_version,
        material_fingerprint = fingerprint,
        model_version        = eff_config.model_version,
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
            # Marks this as an INVOLUNTARY outage rather than a business
            # decision to close. Exempts the facility from closure cost and from
            # contractual must-remain-open constraints (which would otherwise
            # render every disruption of a contracted facility infeasible).
            "is_disruption_target":                 True,
        }) if f.id == facility_id else f
        for f in network.facilities
    ]

    return network.model_copy(update={"facilities": new_facilities})


# ---------------------------------------------------------------------------
# REI normalisation (pure arithmetic — no MILP, no LLM)
# ---------------------------------------------------------------------------

def economic_impact_of(performance_impact: Optional[float]) -> Optional[float]:
    """
    Positive economic exposure implied by a Performance Impact.

        economic_impact_i = max(0, PI_i)

    Floored at zero because a disruption that *reduces* optimized cost
    represents no economic exposure to losing that facility. The raw signed PI
    is retained separately and flagged — the flooring applies only to the
    quantity REI normalises over, so the anomaly is never hidden, merely kept
    out of the ranking where it would invert the order.
    """
    if performance_impact is None:
        return None
    return max(0.0, performance_impact)


def normalize_rei(
    performance_impacts: Sequence[Optional[float]],
) -> Tuple[List[Optional[float]], Optional[float], REIStatus]:
    """
    Normalise Performance Impacts into Risk Exposure Indices.

        economic_impact_i = max(0, PI_i)
        REI_i             = economic_impact_i / max_j(economic_impact_j)

    Guarantees, all asserted by tests:

        0 ≤ REI_i ≤ 1                       for every assessed facility
        REI = 1                             for the largest positive exposure
        REI = 0                             where PI ≤ 0 (no exposure)
        REI = None                          where the facility could not be assessed
        no division by zero                 when every impact is zero

    The [0, 1] bound is a hard requirement: REI feeds a future
    RF = P + REI − P·REI, which is only defined on the unit interval.

    A NEGATIVE PI does NOT produce a negative REI. It produces REI = 0 (no
    exposure) while the signed PI remains visible on the result for
    investigation — see `economic_impact_of`.

    Args:
        performance_impacts: signed PI per facility, None where unavailable.

    Returns:
        (reis, max_economic_impact, status) — `reis` aligns positionally.
    """
    impacts = [economic_impact_of(pi) for pi in performance_impacts]
    known = [i for i in impacts if i is not None]

    if not known:
        return [None for _ in performance_impacts], None, REIStatus.NOT_COMPUTED

    max_impact = max(known)

    if max_impact <= 0.0:
        # Every impact is zero: no facility increases cost when disrupted.
        # No division is performed.
        return (
            [None if i is None else 0.0 for i in impacts],
            0.0,
            REIStatus.NO_RELATIVE_COST_EXPOSURE,
        )

    return (
        [None if i is None else i / max_impact for i in impacts],
        max_impact,
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
    diag_config = shortage_config(disrupted_network, baseline.effective_config)

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
    batch_id:          Optional[str] = None,
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

    # Provenance carried on EVERY row, whatever the outcome, so an unusable
    # result is as traceable as a usable one.
    common = dict(
        facility_id               = facility_id,
        facility_name             = target.name,
        facility_role             = target.role.value,
        network_id                = network.network_id,
        data_version              = network.data_version,
        network_snapshot_id       = baseline.snapshot_id,
        model_version             = baseline.model_version,
        batch_id                  = batch_id,
        scenario_id               = f"REI_DISRUPT_{facility_id}",
        calculation_timestamp     = _utc_now(),
        disruption_type           = disruption_config.disruption_type.value,
        disruption_period         = disruption_config.disruption_period.value,
        baseline_business_cost    = baseline.cost,
        baseline_served           = baseline.served,
        baseline_carbon           = baseline.carbon,
        baseline_solver_objective = baseline.business_cost.solver_objective,
        solve_seconds             = elapsed,
    )

    # ---- Unverified result: TIME_LIMIT is NOT a valid REI ------------------
    # `is_solved` accepts TIME_LIMIT because a time-limited incumbent is still
    # useful for reporting. It is NOT acceptable as a REI input: the cost is not
    # proven optimal, so the incremental cost against a proven-optimal baseline
    # would be measuring solver effort, not exposure.
    if disrupted_result.solver.status == SolverStatus.TIME_LIMIT:
        diagnostics.append(
            f"Disruption of '{facility_id}' hit the solver time limit. The incumbent is "
            f"not proven optimal, so comparing it against a proven-optimal baseline would "
            f"measure solver effort rather than exposure. REI is reported as unavailable "
            f"rather than computed from an unverified cost."
        )
        res = FacilityResilienceResult(
            solver_status          = disrupted_result.solver.status,
            is_feasible            = True,   # a feasible incumbent does exist
            rei_status             = REIStatus.NOT_COMPUTED,
            calculation_status     = CalculationStatus.TIME_LIMIT,
            failure_reason         = "solver time limit reached; result unverified",
            solver_runtime_seconds = disrupted_result.solver.runtime_seconds,
            optimality_gap         = disrupted_result.solver.mip_gap,
            disrupted_solver_objective = disrupted_result.solver.objective_value,
            diagnostics            = diagnostics,
            **common,
        )
        res.risk_classification = classify_risk(res, disruption_config)
        logger.warning(
            "resilience.facility.time_limit facility_id=%s runtime_s=%s gap=%s",
            facility_id, disrupted_result.solver.runtime_seconds,
            disrupted_result.solver.mip_gap,
        )
        return res

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
            calculation_status         = (
                CalculationStatus.INFEASIBLE
                if disrupted_result.solver.status == SolverStatus.INFEASIBLE
                else CalculationStatus.ERROR
            ),
            failure_reason             = (
                f"disruption produced solver status "
                f"{disrupted_result.solver.status.value}"
            ),
            solver_runtime_seconds     = disrupted_result.solver.runtime_seconds,
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
            solver_status      = disrupted_result.solver.status,
            is_feasible        = False,
            rei_status         = REIStatus.NOT_COMPUTED,
            calculation_status = CalculationStatus.ERROR,
            failure_reason     = f"business cost reconciliation failed: {exc}",
            diagnostics        = diagnostics,
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
        economic_impact            = economic_impact_of(performance_impact),
        cost_impact_pct            = cost_impact_pct,
        calculation_status         = CalculationStatus.OK,
        solver_runtime_seconds     = disrupted_result.solver.runtime_seconds,
        optimality_gap             = disrupted_result.solver.mip_gap,
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
    batch_id:          Optional[str] = None,
    snapshot_id:       Optional[str] = None,
    max_workers:       int = 1,
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
                           caching, async or remote (Azure) execution, with no
                           interface change.
        batch_id:          Identifier stamped on every row. Generated if omitted.
        snapshot_id:       Immutable snapshot identity, for traceability.
        max_workers:       1 (default) runs scenarios sequentially. >1 runs them
                           on a thread pool — scenarios are independent by
                           construction, each operating on its own deep-copied
                           network. Threads genuinely help: highspy releases the
                           GIL inside the solve, measured at ~1.85x on 4 workers
                           for the Case-16 fixture. Left at 1 by default so
                           behaviour is predictable unless a caller opts in.

    Returns:
        FacilityResilienceRegistry, ranked by descending Performance Impact.
        A failure in one scenario is ISOLATED: that node is recorded as failed
        and every other node still yields a valid REI.

    Raises:
        BaselineSolveError:        the baseline is infeasible.
        NoEligibleFacilitiesError: no facility qualifies for assessment.
    """
    if config is None:
        config = network.config
    if disruption_config is None:
        disruption_config = DisruptionConfig()
    solve_fn = solve_fn or _default_solve
    batch_id = batch_id or f"rei_{uuid.uuid4().hex[:12]}"

    batch_started = time.perf_counter()
    started_at = _utc_now()
    warnings_list: List[str] = []

    baseline = compute_baseline(
        network, config, disruption_config, solve_fn=solve_fn, snapshot_id=snapshot_id,
    )
    warnings_list.extend(baseline.business_cost.notes)

    facilities = discover_eligible_facilities(network, disruption_config, baseline.result)
    if not facilities:
        # Two very different causes produce an empty list, and they used to
        # produce the same message.
        #
        # (a) The FILTERS exclude everything — asking for DCs in a network that
        #     has none. The configuration is wrong, and naming it is the fix.
        #
        # (b) The BASELINE opened nothing. `is_solved` accepts SolverStatus
        #     TIME_LIMIT, so a solve that ran out of time before finding any
        #     incumbent returns "solved" with every facility closed, and
        #     `only_baseline_open_facilities` then filters out a network that
        #     is perfectly assessable. The message blamed the filters and sent
        #     the reader to look at a configuration that was correct.
        #
        # Distinguished by re-running the filters WITHOUT the baseline
        # restriction: if that finds facilities, the baseline is the cause.
        unrestricted = discover_eligible_facilities(
            network,
            disruption_config.model_copy(
                update={"only_baseline_open_facilities": False}),
            None,
        )
        if unrestricted and disruption_config.only_baseline_open_facilities:
            status = getattr(baseline.result.solver.status, "value",
                             str(baseline.result.solver.status))
            raise NoEligibleFacilitiesError(
                f"The baseline solve of network '{network.network_id}' opened no "
                f"facility, so there is nothing whose loss can be measured — but "
                f"{len(unrestricted)} facility(ies) match the configured filters. "
                f"The baseline, not the configuration, is what produced nothing "
                f"(solver status: {status}"
                + (f", MIP gap {baseline.result.solver.mip_gap}"
                   if baseline.result.solver.mip_gap is not None else "")
                + "). A solve that reached its time limit before finding any "
                f"incumbent reports as solved with every site closed; re-run with "
                f"a longer time limit, or set only_baseline_open_facilities=False "
                f"to assess the footprint as declared."
            )
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

    def assess_one(fac: FacilityRecord) -> FacilityResilienceResult:
        """
        Assess one facility, converting ANY failure into a recorded row.

        Catching broadly is deliberate: one scenario blowing up must not destroy
        a batch in which every other node solved cleanly. Nothing is swallowed —
        the failure becomes an explicit ERROR row carrying its reason, and the
        batch status reflects it.
        """
        try:
            return assess_facility_resilience(
                network, config, fac.id, disruption_config,
                baseline=baseline, solve_fn=solve_fn, batch_id=batch_id,
            )
        except Exception as exc:  # noqa: BLE001 - isolated, recorded, never hidden
            logger.error(
                "resilience.facility.failed facility_id=%s error_type=%s error=%s",
                fac.id, type(exc).__name__, exc,
            )
            return FacilityResilienceResult(
                facility_id            = fac.id,
                facility_name          = fac.name,
                facility_role          = fac.role.value,
                network_id             = network.network_id,
                data_version           = network.data_version,
                network_snapshot_id    = baseline.snapshot_id,
                model_version          = baseline.model_version,
                batch_id               = batch_id,
                calculation_timestamp  = _utc_now(),
                calculation_status     = CalculationStatus.ERROR,
                failure_reason         = f"{type(exc).__name__}: {exc}",
                disruption_type        = disruption_config.disruption_type.value,
                disruption_period      = disruption_config.disruption_period.value,
                baseline_business_cost = baseline.cost,
                solver_status          = SolverStatus.ERROR,
                is_feasible            = False,
                rei_status             = REIStatus.NOT_COMPUTED,
                risk_classification    = RiskClassification.UNKNOWN,
                diagnostics            = [f"Assessment error: {type(exc).__name__}: {exc}"],
            )

    if max_workers > 1 and len(facilities) > 1:
        # Scenarios are independent: each builds its own deep-copied network and
        # shares only the read-only baseline. pool.map preserves input order, so
        # the result is identical to a sequential run.
        with ThreadPoolExecutor(max_workers=min(max_workers, len(facilities))) as pool:
            results: List[FacilityResilienceResult] = list(pool.map(assess_one, facilities))
    else:
        results = [assess_one(fac) for fac in facilities]

    for res in results:
        if res.calculation_status != CalculationStatus.OK:
            warnings_list.append(
                f"{res.facility_id}: {res.calculation_status.value}"
                + (f" - {res.failure_reason}" if res.failure_reason else "")
            )

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
    n_successful = sum(1 for r in results if r.calculation_status == CalculationStatus.OK)
    n_failed = sum(1 for r in results
                   if r.calculation_status in (CalculationStatus.ERROR,
                                               CalculationStatus.TIME_LIMIT))
    total_seconds = round(time.perf_counter() - batch_started, 4)
    n_milp_solves = 1 + len(results) + n_diagnostic

    # Batch status: partial failure is reported, not hidden behind a green light,
    # and not escalated to a total failure when usable results exist.
    if n_successful == 0:
        batch_status = REIBatchStatus.FAILED
    elif n_successful < len(results):
        batch_status = REIBatchStatus.COMPLETED_WITH_ERRORS
    else:
        batch_status = REIBatchStatus.COMPLETED

    logger.info(
        "resilience.registry.ranking_completed network_id=%s batch_id=%s status=%s "
        "n_assessed=%d n_successful=%d n_infeasible=%d n_failed=%d "
        "n_milp_solves=%d baseline_solve_s=%.4f total_s=%.4f",
        network.network_id, batch_id, batch_status.value, len(results), n_successful,
        n_infeasible, n_failed, n_milp_solves, baseline.solve_seconds, total_seconds,
    )

    return FacilityResilienceRegistry(
        network_id                = network.network_id,
        data_version              = network.data_version,
        batch_id                  = batch_id,
        network_snapshot_id       = baseline.snapshot_id,
        material_fingerprint      = baseline.material_fingerprint,
        model_version             = baseline.model_version,
        batch_status              = batch_status,
        started_at                = started_at,
        completed_at              = _utc_now(),
        n_successful              = n_successful,
        n_failed                  = n_failed,
        n_milp_solves             = n_milp_solves,
        served_from_cache         = False,
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
