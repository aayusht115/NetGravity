"""
Orchestrator — Deterministic Result Observation Layer.

Phase 8.6 result observation layer. Inspects `AgentResult` and `ExecutionContext`
evidence to produce a typed `ResultObservation`.

INVARIANTS:
  - Deterministic evaluation: same inputs -> same observation.
  - Never infers authoritative numerical values from free-form LLM text.
  - Preserves mathematical findings (e.g., solver infeasibility, missing data gaps).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from netgravity.orchestrator.core.planner import (
    CAP_FORECAST,
    CAP_OPTIMIZE,
    CAP_OPTIMIZE_SCEN,
)
from netgravity.orchestrator.schemas.adaptive import (
    AdaptiveExecutionConfig,
    ResultObservation,
)
from netgravity.orchestrator.schemas.agent_result import AgentResult
from netgravity.orchestrator.schemas.plans import AgentStatus, PlanStep

logger = logging.getLogger(__name__)


class ResultObserver:
    """
    Interprets execution outcomes deterministically without LLM assistance.
    """

    def __init__(
        self,
        config: Optional[AdaptiveExecutionConfig] = None,
        snapshots: Optional[Any] = None,
    ) -> None:
        self.config = config or AdaptiveExecutionConfig()
        self.snapshots = snapshots

    def observe(
        self,
        step: PlanStep,
        result: AgentResult,
        context: Any,
    ) -> ResultObservation:
        """
        Produce a structured observation of a step result and its domain significance.
        """
        status = result.status
        is_usable = result.is_usable
        domain_outcome = "STANDARD_SUCCESS"
        summary = f"Step '{step.step_id}' ({step.capability}) finished with status {status.value}."
        requires_replanning = False
        requires_human_escalation = False
        metadata: Dict[str, Any] = {}

        # ------------------------------------------------------------------
        # 1. Success / Partial Domain Interpretation
        # ------------------------------------------------------------------
        if status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL):
            if step.capability in (CAP_FORECAST, "forecast.demand"):
                material_change, growth_val = self._evaluate_forecast_materiality(result, context)
                metadata["growth_rate"] = growth_val
                if material_change:
                    domain_outcome = "MATERIAL_FORECAST_INCREASE"
                    summary = (
                        f"Demand forecast for step '{step.step_id}' indicated material demand change "
                        f"({growth_val:+.1%}), exceeding threshold of {self.config.material_forecast_threshold:.1%}."
                    )
                    requires_replanning = True
                else:
                    domain_outcome = "FLAT_FORECAST"
                    summary = (
                        f"Demand forecast for step '{step.step_id}' indicated stable demand "
                        f"({growth_val:+.1%}), within normal threshold."
                    )

            elif step.capability in (CAP_OPTIMIZE, CAP_OPTIMIZE_SCEN, "optimization.solve", "optimization.solve_scenario"):
                if self._is_optimization_infeasible(result):
                    domain_outcome = "INFEASIBLE_OPTIMIZATION"
                    summary = f"Optimization in step '{step.step_id}' is mathematically infeasible."
                    requires_human_escalation = True
                    is_usable = False
                else:
                    domain_outcome = "FEASIBLE_OPTIMIZATION"
                    summary = f"Optimization in step '{step.step_id}' solved to mathematical optimality."

            else:
                domain_outcome = "STANDARD_SUCCESS"

        elif status == AgentStatus.PARTIAL:
            domain_outcome = "PARTIAL_SUCCESS"
            summary = (
                f"Step '{step.step_id}' ({step.capability}) completed with partial evidence; "
                f"downstream consumers received explicit unavailable evidence records."
            )

        # ------------------------------------------------------------------
        # 2. Failure & Gap Handling
        # ------------------------------------------------------------------
        elif status == AgentStatus.INSUFFICIENT_EVIDENCE:
            domain_outcome = "INSUFFICIENT_EVIDENCE"
            err_msg = result.errors[0].message if result.errors else "Prerequisites unavailable"
            summary = f"Step '{step.step_id}' ({step.capability}) lacks sufficient evidence: {err_msg}."

        elif status == AgentStatus.RETRYABLE_FAILURE:
            domain_outcome = "RETRYABLE_FAILURE"
            err_code = result.errors[0].code if result.errors else "TRANSIENT_ERROR"
            summary = f"Transient failure on step '{step.step_id}' ({step.capability}): {err_code}."

        elif status == AgentStatus.NON_RETRYABLE_FAILURE:
            if self._is_optimization_infeasible(result):
                domain_outcome = "INFEASIBLE_OPTIMIZATION"
                summary = f"Optimization in step '{step.step_id}' proved mathematically infeasible."
                requires_human_escalation = True
            else:
                domain_outcome = "NON_RETRYABLE_FAILURE"
                err_msg = result.errors[0].message if result.errors else "Deterministic engine failure"
                summary = f"Non-retryable failure on step '{step.step_id}' ({step.capability}): {err_msg}."

        elif status == AgentStatus.INVALID_OUTPUT:
            domain_outcome = "INVALID_OUTPUT"
            summary = f"Output schema validation failed for step '{step.step_id}' ({step.capability})."

        return ResultObservation(
            step_id=step.step_id,
            capability=step.capability,
            status=status,
            is_usable=is_usable,
            domain_outcome=domain_outcome,
            summary=summary,
            requires_replanning=requires_replanning,
            requires_human_escalation=requires_human_escalation,
            metadata=metadata,
        )

    def _evaluate_forecast_materiality(self, result: AgentResult, context: Any) -> tuple[bool, float]:
        """
        Check whether forecast results show a material demand deviation >= threshold.
        """
        forecast_obj = getattr(context, "forecast_result", None) or result.output
        if not forecast_obj:
            return False, 0.0

        growth_rate = 0.0

        # Attempt to find baseline demands from self.snapshots or context
        base_vols: Dict[str, float] = {}
        target_snap_id = getattr(context, "baseline_snapshot_id", None)
        if target_snap_id:
            snap = None
            if self.snapshots is not None and hasattr(self.snapshots, "get"):
                try:
                    snap = self.snapshots.get(target_snap_id)
                except Exception:
                    pass
            elif hasattr(context, "snapshots") and hasattr(context.snapshots, "get"):
                try:
                    snap = context.snapshots.get(target_snap_id)
                except Exception:
                    pass

            if snap and hasattr(snap, "network") and hasattr(snap.network, "demands"):
                for d in snap.network.demands:
                    base_vols[d.market_id] = float(getattr(d, "quantity", getattr(d, "volume", 0.0)))

        if isinstance(forecast_obj, dict):
            # Check explicit growth rate or deltas
            if "growth_rate" in forecast_obj:
                try:
                    growth_rate = float(forecast_obj["growth_rate"])
                except (ValueError, TypeError):
                    growth_rate = 0.0
            elif "percentage_change" in forecast_obj:
                try:
                    growth_rate = float(forecast_obj["percentage_change"])
                except (ValueError, TypeError):
                    growth_rate = 0.0
            elif "forecast_demand" in forecast_obj and "baseline_demand" in forecast_obj:
                try:
                    base = float(forecast_obj["baseline_demand"])
                    f_val = float(forecast_obj["forecast_demand"])
                    if base > 0:
                        growth_rate = (f_val - base) / base
                except (ValueError, TypeError, ZeroDivisionError):
                    growth_rate = 0.0
            elif "material_increase" in forecast_obj:
                if bool(forecast_obj["material_increase"]):
                    return True, 0.20
            elif "series" in forecast_obj and isinstance(forecast_obj["series"], list):
                for s in forecast_obj["series"]:
                    if isinstance(s, dict):
                        mkt = s.get("market_id", "")
                        b_vol = base_vols.get(mkt)
                        if "growth_rate" in s:
                            try:
                                growth_rate = max(growth_rate, float(s["growth_rate"]))
                            except (ValueError, TypeError):
                                pass
                        elif "points" in s and s["points"]:
                            for p in s["points"]:
                                b_mean = p.get("baseline_mean") or b_vol
                                f_mean = p.get("mean") or p.get("p50")
                                if b_mean and f_mean and b_mean > 0:
                                    g = (float(f_mean) - float(b_mean)) / float(b_mean)
                                    growth_rate = max(growth_rate, g)

        # Check typed ForecastResult object
        elif hasattr(forecast_obj, "series"):
            for s in getattr(forecast_obj, "series", []):
                mkt = getattr(s, "market_id", "")
                b_vol = base_vols.get(mkt)
                s_dict = s if isinstance(s, dict) else getattr(s, "__dict__", {})
                if "growth_rate" in s_dict:
                    try:
                        growth_rate = max(growth_rate, float(s_dict.get("growth_rate", 0.0)))
                    except (ValueError, TypeError):
                        pass
                elif hasattr(s, "points"):
                    for p in getattr(s, "points", []):
                        b_mean = getattr(p, "baseline_mean", None) or b_vol
                        f_mean = getattr(p, "mean", None) or getattr(p, "p50", None)
                        if b_mean and f_mean and b_mean > 0:
                            g = (float(f_mean) - float(b_mean)) / float(b_mean)
                            growth_rate = max(growth_rate, g)

        is_material = abs(growth_rate) >= self.config.material_forecast_threshold
        return is_material, growth_rate

    def _is_optimization_infeasible(self, result: AgentResult) -> bool:
        """
        Verify if an optimization result explicitly contains an infeasible solver finding.
        """
        if result.errors:
            for err in result.errors:
                if "INFEASIBLE" in (err.code or "").upper() or "INFEASIBLE" in (err.message or "").upper():
                    return True

        if isinstance(result.output, dict):
            status = str(result.output.get("status", "")).lower()
            feasible = result.output.get("feasible")
            if feasible is False or "infeasible" in status:
                return True

        return False
