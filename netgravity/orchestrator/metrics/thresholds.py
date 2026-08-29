"""
Orchestrator — The verified threshold catalogue.

Every value below was read from the CURRENT source file it claims, by a
dedicated verification pass over the codebase (see
`validation/kpi_authoritative_layer/report.md` §Audit for the full
methodology). A separate, pre-existing audit document
(`docs/kpi_formula_threshold_action_audit.md`) claimed a materially different
set of thresholds — a governance system organised around three rupee-valued
"Tiers", an `HIGH_RISK_THRESHOLD=0.70`, `DC_CAPACITY_STRESS=85%`, a 15% demand
surge that "automatically initiates full network optimization re-solve", and a
CUSUM change-point statistic. None of that exists in code. This module
reflects what the source actually contains, not what that document described.

NOTHING here changes a threshold. Every value is imported from — or, where a
literal must be quoted for documentation, copied verbatim and covered by a test
that re-imports the real constant and asserts equality — the module that
already owns it. If a constant changes there, the test in this module's test
suite fails until this catalogue is updated, rather than silently drifting.
"""

from __future__ import annotations

from typing import List

from netgravity.orchestrator.schemas.kpi import ThresholdBasis, ThresholdSpec


def _governance_thresholds() -> List[ThresholdSpec]:
    """
    From `netgravity.orchestrator.governance.action_classifier.GovernancePolicy`.

    Real classification values are AUTO_ACTION / APPROVAL_REQUIRED / HUMAN_ONLY
    / NO_ACTION (`orchestrator/schemas/actions.py::ActionClassification`) — there
    is no "Tier 1/2/3" concept and no rupee-valued threshold anywhere in the
    classifier. Cost is judged as a PERCENTAGE impact, never an absolute figure.
    """
    from netgravity.orchestrator.governance.action_classifier import GovernancePolicy
    p = GovernancePolicy()
    return [
        ThresholdSpec(
            threshold_id="GOV_RISK_FACTOR_HUMAN", metric_id="risk_factor",
            operator=">=", value=p.risk_factor_human, unit="ratio [0,1]",
            severity="HUMAN_ONLY", basis=ThresholdBasis.BUSINESS_POLICY,
            action_if_triggered="Action classified HUMAN_ONLY (rule R6): no "
                               "automated or approval-gated path is offered.",
            source_file="orchestrator/governance/action_classifier.py:110",
        ),
        ThresholdSpec(
            threshold_id="GOV_RISK_FACTOR_APPROVAL", metric_id="risk_factor",
            operator=">=", value=p.risk_factor_approval, unit="ratio [0,1]",
            severity="APPROVAL_REQUIRED", basis=ThresholdBasis.BUSINESS_POLICY,
            action_if_triggered="Action classified APPROVAL_REQUIRED (rule R8) "
                               "unless a higher-precedence rule already fired.",
            source_file="orchestrator/governance/action_classifier.py:112",
        ),
        ThresholdSpec(
            threshold_id="GOV_COST_IMPACT_HUMAN", metric_id="cost_impact_pct",
            operator=">=", value=p.cost_impact_human_pct, unit="%",
            severity="HUMAN_ONLY", basis=ThresholdBasis.BUSINESS_POLICY,
            action_if_triggered="Action classified HUMAN_ONLY (rule R5).",
            source_file="orchestrator/governance/action_classifier.py:106",
        ),
        ThresholdSpec(
            threshold_id="GOV_COST_IMPACT_APPROVAL", metric_id="cost_impact_pct",
            operator=">=", value=p.cost_impact_approval_pct, unit="%",
            severity="APPROVAL_REQUIRED", basis=ThresholdBasis.BUSINESS_POLICY,
            action_if_triggered="Action classified APPROVAL_REQUIRED (rule R9) "
                               "unless a higher-precedence rule already fired.",
            source_file="orchestrator/governance/action_classifier.py:104",
        ),
        ThresholdSpec(
            threshold_id="GOV_UNSERVED_DEMAND_HUMAN", metric_id="unserved_demand_rate",
            operator=">", value=p.unserved_demand_human_rate, unit="fraction [0,1]",
            severity="HUMAN_ONLY", basis=ThresholdBasis.BUSINESS_POLICY,
            action_if_triggered="Action classified HUMAN_ONLY (rule R4).",
            source_file="orchestrator/governance/action_classifier.py:108",
        ),
        # Structural actions (OPEN/CLOSE_FACILITY) are HUMAN_ONLY unconditionally
        # (rule R2) — irreversibility governs, not a measured threshold. Not
        # represented as a ThresholdSpec because there is no numeric comparison;
        # documented here so its absence from this list is not mistaken for an
        # omission. See action_classifier.py:324-331.
    ]


def _risk_classification_thresholds() -> List[ThresholdSpec]:
    """
    From `netgravity.schemas.resilience.RiskClassificationRules`.

    Disabled (`None`) by default, and deliberately so: the module's own
    docstring states there is no documented business basis for an absolute REI
    band, so none is applied unless an operator configures one. Only
    `infeasible_is_critical` (a boolean, not a numeric comparison) is on by
    default.
    """
    from netgravity.schemas.resilience import RiskClassificationRules
    r = RiskClassificationRules()
    return [
        ThresholdSpec(
            threshold_id="REI_UNSERVED_DEMAND_CRITICAL", metric_id="unserved_demand_rate",
            operator=">=", value=r.unserved_demand_rate_critical, unit="fraction [0,1]",
            severity="CRITICAL", basis=ThresholdBasis.UNCONFIGURED,
            action_if_triggered="Facility risk_classification = CRITICAL, "
                               "when configured (disabled by default).",
            source_file="schemas/resilience.py:151", configurable=True,
        ),
        ThresholdSpec(
            threshold_id="REI_UNSERVED_DEMAND_HIGH", metric_id="unserved_demand_rate",
            operator=">=", value=r.unserved_demand_rate_high, unit="fraction [0,1]",
            severity="HIGH", basis=ThresholdBasis.UNCONFIGURED,
            action_if_triggered="Facility risk_classification = HIGH, when "
                               "configured (disabled by default).",
            source_file="schemas/resilience.py:152", configurable=True,
        ),
        ThresholdSpec(
            threshold_id="REI_COST_IMPACT_HIGH", metric_id="cost_impact_pct",
            operator=">=", value=r.cost_impact_pct_high, unit="%",
            severity="HIGH", basis=ThresholdBasis.UNCONFIGURED,
            action_if_triggered="Facility risk_classification = HIGH, when "
                               "configured (disabled by default).",
            source_file="schemas/resilience.py:155", configurable=True,
        ),
    ]


def _utilization_thresholds() -> List[ThresholdSpec]:
    """
    From `netgravity.config.defaults.UTILIZATION_THRESHOLDS`.

    Governs `NetworkKPIs.overutilized_count` / `.underutilized_count`
    (`netgravity/metrics/kpis.py:152-155`) — a count, not an alert or an
    insight. No deterministic insight-generation engine exists in this
    codebase (verified); these thresholds only classify facilities into the
    two counted buckets.
    """
    from netgravity.config.defaults import UTILIZATION_THRESHOLDS as UT
    return [
        ThresholdSpec(
            threshold_id="UTILIZATION_OVER", metric_id="utilization_pct",
            operator=">=", value=UT["over_threshold"] * 100.0, unit="%",
            severity="OVERUTILIZED", basis=ThresholdBasis.ENGINEERING,
            action_if_triggered="Facility counted in NetworkKPIs.overutilized_count.",
            source_file="config/defaults.py:75",
        ),
        ThresholdSpec(
            threshold_id="UTILIZATION_UNDER", metric_id="utilization_pct",
            operator="<=", value=UT["under_threshold"] * 100.0, unit="%",
            severity="UNDERUTILIZED", basis=ThresholdBasis.ENGINEERING,
            action_if_triggered="Facility counted in NetworkKPIs.underutilized_count.",
            source_file="config/defaults.py:76",
        ),
    ]


def _go_no_go_thresholds() -> List[ThresholdSpec]:
    """From `netgravity.config.defaults.GO_NO_GO_DEFAULTS`."""
    from netgravity.config.defaults import GO_NO_GO_DEFAULTS as GNG
    return [
        ThresholdSpec(
            threshold_id="GO_NO_GO_SAVINGS", metric_id="annual_savings",
            operator=">=", value=GNG["savings_threshold"], unit="currency",
            severity="GO", basis=ThresholdBasis.BUSINESS_POLICY,
            action_if_triggered="Savings criterion for a GO recommendation is met.",
            source_file="config/defaults.py:93",
        ),
        ThresholdSpec(
            threshold_id="GO_NO_GO_SERVICE", metric_id="service_delta",
            operator=">=", value=GNG["service_delta_threshold"], unit="fraction",
            severity="GO", basis=ThresholdBasis.BUSINESS_POLICY,
            action_if_triggered="Service-degradation criterion for a GO "
                               "recommendation is met (service may not drop "
                               "more than 2%).",
            source_file="config/defaults.py:94",
        ),
    ]


def _adaptive_execution_thresholds() -> List[ThresholdSpec]:
    """
    From `netgravity.orchestrator.schemas.adaptive.AdaptiveExecutionConfig`.

    The pre-existing audit misattributed this constant to
    `orchestrator/routing/signal_router.py` and claimed it "automatically
    initiates full network optimization re-solve." Verified: it lives here,
    defaults DISABLED (`enable_materiality_branching=False`), and even when
    enabled its only effect is a conditional PLAN REPLAN (adding
    scenario/resilience steps) — never an automatic re-solve.
    """
    from netgravity.orchestrator.schemas.adaptive import AdaptiveExecutionConfig
    c = AdaptiveExecutionConfig()
    return [
        ThresholdSpec(
            threshold_id="ADAPTIVE_MATERIAL_FORECAST_INCREASE",
            metric_id="forecast_growth_rate",
            operator=">=",
            value=(c.material_forecast_threshold if c.enable_materiality_branching else None),
            unit="fraction",
            severity="REPLAN",
            basis=(ThresholdBasis.BUSINESS_POLICY if c.enable_materiality_branching
                  else ThresholdBasis.UNCONFIGURED),
            action_if_triggered="Triggers a conditional plan REPLAN adding "
                               "scenario/resilience steps (result_observer.py, "
                               "adaptive_policy.py) — NOT an automatic re-solve. "
                               "Disabled by default.",
            source_file="orchestrator/schemas/adaptive.py:108-109",
        ),
    ]


def _circuit_breaker_thresholds() -> List[ThresholdSpec]:
    """
    From `netgravity.orchestrator.core.circuit_breaker.CircuitBreaker` defaults.

    The one section of the pre-existing audit's threshold catalogue that
    checked out exactly against source on first read.
    """
    return [
        ThresholdSpec(
            threshold_id="CIRCUIT_BREAKER_FAILURE_COUNT", metric_id="consecutive_failures",
            operator=">=", value=3.0, unit="count",
            severity="OPEN", basis=ThresholdBasis.ENGINEERING,
            action_if_triggered="Circuit transitions CLOSED -> OPEN; only "
                               "infrastructure failures count (deterministic "
                               "business failures like infeasibility never do).",
            source_file="orchestrator/core/circuit_breaker.py:45", configurable=True,
        ),
        ThresholdSpec(
            threshold_id="CIRCUIT_BREAKER_COOLDOWN", metric_id="seconds_since_open",
            operator=">=", value=30.0, unit="seconds",
            severity="HALF_OPEN", basis=ThresholdBasis.ENGINEERING,
            action_if_triggered="Circuit transitions OPEN -> HALF_OPEN, "
                               "permitting one probe attempt.",
            source_file="orchestrator/core/circuit_breaker.py:46", configurable=True,
        ),
    ]


def _structural_break_thresholds() -> List[ThresholdSpec]:
    """
    From `netgravity.forecasting.change_point` module constants.

    A STATISTICAL basis, not a business or engineering one: these are Andrews
    (1993) asymptotic critical values for the sup-F / Quandt-Andrews test, not
    a tunable business rule. The pre-existing audit invented an entirely
    different methodology (CUSUM, k=0.5*std, h=4.0*std) that does not exist
    anywhere in this codebase; verified by direct read and repo-wide grep.
    """
    from netgravity.forecasting.change_point import SUP_F_STRONG, SUP_F_THRESHOLD
    return [
        ThresholdSpec(
            threshold_id="SUP_F_BREAK_DETECTED", metric_id="sup_f_statistic",
            operator=">", value=SUP_F_THRESHOLD, unit="F-statistic",
            severity="DETECTED", basis=ThresholdBasis.STATISTICAL,
            action_if_triggered="A structural break is reported (subject to "
                               "the materiality gates on the fitted step, not "
                               "on this statistic alone).",
            source_file="forecasting/change_point.py:116", configurable=False,
        ),
        ThresholdSpec(
            threshold_id="SUP_F_STRONG_EVIDENCE", metric_id="sup_f_statistic",
            operator=">", value=SUP_F_STRONG, unit="F-statistic",
            severity="STRONG_EVIDENCE", basis=ThresholdBasis.STATISTICAL,
            action_if_triggered="`ChangePointResult.strong_evidence` flagged "
                               "True — an evidence-strength annotation, not "
                               "an additional gate.",
            source_file="forecasting/change_point.py:119", configurable=False,
        ),
    ]


def build_threshold_catalogue() -> List[ThresholdSpec]:
    """The complete, verified threshold catalogue."""
    return [
        *_governance_thresholds(),
        *_risk_classification_thresholds(),
        *_utilization_thresholds(),
        *_go_no_go_thresholds(),
        *_adaptive_execution_thresholds(),
        *_circuit_breaker_thresholds(),
        *_structural_break_thresholds(),
    ]


__all__ = ["build_threshold_catalogue"]
