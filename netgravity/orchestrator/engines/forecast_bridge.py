"""
Orchestrator — turning a forecast into a MILP input.

    ForecastResult  →  [ validate ]  →  CanonicalNetwork  →  MILP

This lives in the ORCHESTRATOR, not in the forecasting package, and that
placement is the architecture. The Forecasting Agent is never handed a
`CanonicalNetwork`; it returns estimates and stops. Deciding that an estimate is
fit to optimise against — fresh enough, complete enough, for the right snapshot
— is a control-plane judgement, and it happens here.

A test asserts that `netgravity.forecasting` imports neither `CanonicalNetwork`
nor the solver, so the boundary holds structurally rather than by convention.

── Two defects this module exists to prevent ──────────────────────────────────
Both were live in the source repository's `bridge/milp_bridge.py`, and both are
reproduced as tests.

**1. Unforecast demand vanished.** `update_network_with_forecast` rebuilt the
demand list from the forecasts alone. Any market-product the forecaster had not
covered was silently deleted from the network — not zeroed, *removed*. The MILP
then optimised a smaller problem and reported a lower cost, with nothing
anywhere indicating demand had gone missing. Measured on a two-market network,
forecasting one market dropped the other's 250 units entirely.

**2. The forecast network impersonated the observed one.**
`network.model_copy(update={"demands": ...})` leaves `data_version` untouched,
so a network carrying forecast demand still advertised the observed data
version. Since `SnapshotManager` keys snapshots on `snap_` + `data_version[:12]`
and returns the existing record on a hit, registering a forecast network
returned the OBSERVED snapshot and discarded the forecast — verified against
this codebase's own store. Every network built here recomputes its data version,
so a forecast can never occupy an observed snapshot's identity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from netgravity.forecasting.schemas import (
    ForecastResult,
    ForecastStatus,
    SeriesForecast,
)
from netgravity.schemas.network import CanonicalNetwork, DemandRecord

logger = logging.getLogger(__name__)


class DemandProvenance(str, Enum):
    """
    Where a demand figure came from.

    The distinction §10 of the integration brief requires, made explicit on
    every record rather than inferred from context.
    """
    OBSERVED = "OBSERVED"           # measured, from the snapshot
    FORECAST = "FORECAST"           # estimated by the Forecasting Agent
    SCENARIO_OVERRIDE = "SCENARIO_OVERRIDE"   # set by a what-if


class UnforecastPolicy(str, Enum):
    """
    What to do about demand the forecast did not cover.

    There is deliberately no policy that drops a record. Both options keep every
    market-product in the network; they differ only in whether the run is
    allowed to proceed with a mixture.
    """
    #: Refuse to build a network. The default — a caller who asked to optimise
    #: against a forecast and got partial coverage should decide what that
    #: means, not discover later that half the demand was observed.
    REJECT = "REJECT"
    #: Keep the observed figure, and name every record it happened to. Explicit
    #: mixing, never silent: the substitutions are listed in the result and
    #: stamped OBSERVED in the provenance map.
    KEEP_OBSERVED = "KEEP_OBSERVED"


class QuantileMode(str, Enum):
    """Which quantile of the forecast becomes the MILP's demand quantity."""
    P50 = "P50"     # median — the default planning case
    MEAN = "MEAN"   # expected value
    P10 = "P10"     # conservative: plan for low demand
    P90 = "P90"     # surge: plan for high demand


@dataclass
class ForecastApplication:
    """
    The outcome of applying a forecast to a network.

    `network` is None whenever `ok` is False. There is no partially-applied
    network — a caller cannot accidentally solve against a half-built one.
    """
    ok: bool
    network: Optional[CanonicalNetwork] = None

    #: (market_id, product_id) → where that demand figure came from.
    provenance: Dict[Tuple[str, str], DemandProvenance] = field(default_factory=dict)
    #: Records that kept their observed value because no forecast covered them.
    substituted_observed: List[str] = field(default_factory=list)
    #: Series the forecast reported as failed, with the reason.
    unavailable: Dict[str, str] = field(default_factory=dict)

    quantile_mode: QuantileMode = QuantileMode.P50
    period: int = 1
    #: Data version of the produced network. Always differs from the observed
    #: one when any demand actually changed.
    data_version: Optional[str] = None
    source_snapshot_id: Optional[str] = None

    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def n_forecast(self) -> int:
        return sum(1 for p in self.provenance.values() if p is DemandProvenance.FORECAST)

    @property
    def n_observed(self) -> int:
        return sum(1 for p in self.provenance.values() if p is DemandProvenance.OBSERVED)

    @property
    def is_mixed(self) -> bool:
        """True when the network blends forecast and observed demand."""
        return self.n_forecast > 0 and self.n_observed > 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_forecast_for_network(
    result: ForecastResult,
    network: CanonicalNetwork,
    *,
    snapshot_id: str,
    period: int = 1,
) -> List[str]:
    """
    Check a forecast may be applied to a network.

    Returns:
        Reasons it may not. Empty means it may.

    Staleness is checked on `snapshot_id` rather than on a timestamp: a forecast
    built from one network's history says nothing dependable about a different
    network, however recently it was produced.
    """
    reasons: List[str] = []

    if result.provenance.snapshot_id != snapshot_id:
        reasons.append(
            f"forecast was produced for snapshot '{result.provenance.snapshot_id}' "
            f"but is being applied to '{snapshot_id}'; a forecast from a different "
            f"network version is stale by construction"
        )

    observed_version = network.data_version
    forecast_version = result.provenance.data_version
    if observed_version and forecast_version and observed_version != forecast_version:
        reasons.append(
            f"forecast was built against data version '{forecast_version[:12]}' but "
            f"the network is at '{observed_version[:12]}'"
        )

    if result.status is not ForecastStatus.OK:
        reasons.append(
            f"forecast status is {result.status.value}, not OK; "
            f"{'; '.join(result.errors) if result.errors else 'no usable series'}"
        )

    if period > result.provenance.horizon:
        reasons.append(
            f"period {period} is beyond the forecast horizon of "
            f"{result.provenance.horizon}"
        )

    if not result.successful:
        reasons.append("the forecast contains no successful series")

    return reasons


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def _quantity_for(series: SeriesForecast, period: int, mode: QuantileMode) -> Optional[Tuple[float, float]]:
    """The (quantity, std_dev) a series contributes for one period, or None."""
    point = series.point(period)
    if point is None:
        return None
    value = {
        QuantileMode.P10: point.p10,
        QuantileMode.P50: point.p50,
        QuantileMode.P90: point.p90,
        QuantileMode.MEAN: point.mean,
    }[mode]
    return float(value), float(point.std_dev)


def apply_forecast_to_network(
    result: ForecastResult,
    network: CanonicalNetwork,
    *,
    snapshot_id: str,
    period: int = 1,
    quantile_mode: QuantileMode = QuantileMode.P50,
    unforecast_policy: UnforecastPolicy = UnforecastPolicy.REJECT,
) -> ForecastApplication:
    """
    Build a MILP-ready network from a forecast.

    Every demand record in the observed network survives into the output. A
    record the forecast covered takes the forecast quantity and standard
    deviation; a record it did not covered is governed by `unforecast_policy`,
    and is never dropped under either setting.

    Service terms — `sla_days`, `service_level`, `priority` — are carried over
    from the observed record. A forecast estimates how much, not how fast; the
    commercial commitment is unchanged by it.
    """
    reasons = validate_forecast_for_network(
        result, network, snapshot_id=snapshot_id, period=period,
    )
    if reasons:
        logger.warning("forecast_bridge.rejected reasons=%s", reasons)
        return ForecastApplication(
            ok=False, reasons=reasons, period=period,
            quantile_mode=quantile_mode, source_snapshot_id=snapshot_id,
        )

    by_key: Dict[Tuple[str, str], SeriesForecast] = {
        (s.market_id, s.product_id): s for s in result.series
    }

    provenance: Dict[Tuple[str, str], DemandProvenance] = {}
    substituted: List[str] = []
    unavailable: Dict[str, str] = {}
    new_demands: List[DemandRecord] = []
    warnings: List[str] = []

    for demand in network.demands:
        key = (demand.market_id, demand.product_id)
        series = by_key.get(key)
        forecast_value = None

        if series is not None and series.ok:
            forecast_value = _quantity_for(series, period, quantile_mode)
            if forecast_value is None:
                unavailable[f"{key[0]}/{key[1]}"] = (
                    f"forecast has no point for period {period}"
                )
        elif series is not None:
            unavailable[f"{key[0]}/{key[1]}"] = f"{series.status.value}: {series.reason}"
        else:
            unavailable[f"{key[0]}/{key[1]}"] = "no forecast was requested for this pair"

        if forecast_value is not None:
            quantity, std_dev = forecast_value
            new_demands.append(demand.model_copy(update={
                "period": period,
                "quantity": round(quantity, 4),
                # Forecast dispersion feeds safety stock through the existing
                # inventory terms. This is the seam the MILP already had for
                # demand uncertainty; no new field was needed.
                "std_dev": round(std_dev, 4),
            }))
            provenance[key] = DemandProvenance.FORECAST
        else:
            # Never dropped. The record survives with its observed value, and
            # the policy decides whether the run may proceed.
            new_demands.append(demand.model_copy(update={"period": period}))
            provenance[key] = DemandProvenance.OBSERVED
            substituted.append(f"{key[0]}/{key[1]}")

    if substituted and unforecast_policy is UnforecastPolicy.REJECT:
        detail = ", ".join(sorted(substituted)[:10])
        more = f" (+{len(substituted) - 10} more)" if len(substituted) > 10 else ""
        return ForecastApplication(
            ok=False,
            reasons=[
                f"{len(substituted)} demand record(s) have no usable forecast: "
                f"{detail}{more}. Under REJECT the network is not built, because "
                f"silently mixing observed and forecast demand would produce an "
                f"optimum nobody could attribute to either."
            ],
            unavailable=unavailable,
            substituted_observed=sorted(substituted),
            period=period, quantile_mode=quantile_mode,
            source_snapshot_id=snapshot_id,
        )

    if substituted:
        warnings.append(
            f"{len(substituted)} demand record(s) kept their OBSERVED value because "
            f"no forecast covered them: {', '.join(sorted(substituted)[:10])}"
            f"{'...' if len(substituted) > 10 else ''}. The resulting network mixes "
            f"forecast and observed demand; see `provenance` for which is which."
        )

    # Give the network its own identity. Without this it would carry the
    # observed data version, collide with the observed snapshot id, and be
    # silently discarded by `SnapshotManager` — verified behaviour, not theory.
    updated = network.model_copy(update={"demands": new_demands, "data_version": None})
    updated = updated.model_copy(update={"data_version": updated.compute_data_version()})

    logger.info(
        "forecast_bridge.applied snapshot=%s period=%d mode=%s forecast=%d observed=%d "
        "data_version=%s",
        snapshot_id, period, quantile_mode.value,
        sum(1 for p in provenance.values() if p is DemandProvenance.FORECAST),
        len(substituted), (updated.data_version or "")[:12],
    )

    return ForecastApplication(
        ok=True,
        network=updated,
        provenance=provenance,
        substituted_observed=sorted(substituted),
        unavailable=unavailable,
        quantile_mode=quantile_mode,
        period=period,
        data_version=updated.data_version,
        source_snapshot_id=snapshot_id,
        warnings=warnings,
    )


def build_quantile_networks(
    result: ForecastResult,
    network: CanonicalNetwork,
    *,
    snapshot_id: str,
    period: int = 1,
    modes: Sequence[QuantileMode] = (QuantileMode.P10, QuantileMode.P50, QuantileMode.P90),
    unforecast_policy: UnforecastPolicy = UnforecastPolicy.REJECT,
) -> Dict[str, ForecastApplication]:
    """
    Build one network per quantile, for robust planning.

    Called BY the orchestrator when a workflow asks for a low/expected/high
    comparison. The Forecasting Agent has no equivalent entry point — the source
    repository put `generate_scenario_variations` on the agent itself, which let
    forecasting decide that three optimisation scenarios should exist. Choosing
    what to analyse is the control plane's decision.
    """
    return {
        mode.value.lower(): apply_forecast_to_network(
            result, network, snapshot_id=snapshot_id, period=period,
            quantile_mode=mode, unforecast_policy=unforecast_policy,
        )
        for mode in modes
    }


__all__ = [
    "DemandProvenance",
    "UnforecastPolicy",
    "QuantileMode",
    "ForecastApplication",
    "validate_forecast_for_network",
    "apply_forecast_to_network",
    "build_quantile_networks",
]
