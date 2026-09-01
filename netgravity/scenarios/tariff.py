"""
NetGravity — Deriving a network's own freight tariff
====================================================
When a scenario proposes a site the client does not operate, that site has no
lanes. Something has to price the freight to and from it, and whatever that
something is decides whether the site ever opens.

The problem this module fixes
-----------------------------
`_auto_connect_facility` priced a new site's lanes at a single rate per km,
estimated as the MEAN OF PER-LANE RATIOS:

    rate_per_km = (1/n) * Σ ( rate_per_unit_i / distance_km_i )

That estimator is dominated by the shortest lanes in the network, because a
last-mile leg of 9 km at ₹9.04 implies ₹0.96/km while a 2,214 km trunk leg at
₹39.92 implies ₹0.018/km — a 53x spread over the SAME tariff. Averaging the
ratios lands near the short end and then multiplies it by a long distance.

Measured on one client network (36 road lanes), that estimator reproduces the
client's OWN lanes with a median error of 419% and a worst case of 730%. A
1,575 km lane the client actually prices at ₹34.17 was quoted at ₹216.86.

The consequence was not a slightly-off number. Every greenfield site was priced
6x out of the market, so the MILP declined to open any of them, whatever
capacity or fixed cost the user proposed. "Open a new DC" always came back
"no change" — which reads as a broken feature, not as an answer.

What is derived instead
-----------------------
Real freight tariffs are affine, not proportional: a pickup/handling component
that does not scale with distance, plus a line-haul component that does.

    rate_per_unit  = fixed_leg_cost   + rate_per_km      * distance_km
    lead_time_days = terminal_time    + distance_km      / speed_km_per_day

Both are fitted by ordinary least squares over the network's own lanes for the
mode in question. On the same client network that is a median error of 8.2% for
the rate and 9.5% for the lead time — the tariff now reproduces the lanes it
was derived from, which is the only evidence available that it will price a new
one sensibly.

Every parameter is the client's. Nothing here contributes a constant to a
number the user will read.

When there is not enough evidence
---------------------------------
Fitting an affine model needs at least three lanes with genuinely different
distances. Below that the fit is not identified, and this module says so rather
than substituting a plausible constant: the previous code fell back to a
hardcoded `0.025` per km, which is a made-up tariff presented in the client's
own currency (brief §24). A caller that receives `is_derived=False` must refuse
the scenario and explain what data would make it answerable.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: An affine fit needs at least this many lanes with distinct distances.
MIN_LANES_FOR_FIT = 3

#: Below this spread (km) the distances are effectively identical and the slope
#: is not identified — a fit would be reading noise.
MIN_DISTANCE_SPREAD_KM = 25.0


@dataclass(frozen=True)
class LaneTariff:
    """
    A freight tariff and transit model derived from one network's own lanes.

    `is_derived` False means the network did not carry enough comparable lanes
    to price a new one. There is deliberately no default tariff to fall back
    on.
    """

    is_derived: bool
    mode: str = "ROAD"
    n_lanes: int = 0

    # rate_per_unit = fixed_leg_cost + rate_per_km * distance_km
    fixed_leg_cost: float = 0.0
    rate_per_km: float = 0.0

    # lead_time_days = terminal_time_days + distance_km / speed_km_per_day
    terminal_time_days: float = 0.0
    speed_km_per_day: float = 0.0

    #: Median absolute percentage error when the fit is asked to reproduce the
    #: lanes it was derived from. Reported so a scenario can state how well the
    #: client's own freight is explained rather than implying exactness.
    rate_fit_error_pct: Optional[float] = None
    lead_time_fit_error_pct: Optional[float] = None

    #: Why no tariff could be derived. Empty when `is_derived`.
    reason: str = ""

    def rate_for(self, distance_km: float) -> float:
        """Freight rate per unit over `distance_km`, at this network's tariff."""
        return max(0.01, round(self.fixed_leg_cost + self.rate_per_km * distance_km, 4))

    def lead_time_for(self, distance_km: float) -> float:
        """Transit days over `distance_km`, at this network's observed pace."""
        if self.speed_km_per_day <= 0:
            return max(0.1, round(self.terminal_time_days, 2))
        return max(0.1, round(
            self.terminal_time_days + distance_km / self.speed_km_per_day, 2))

    def describe(self) -> str:
        """One line, in the client's own units, for an audit trail."""
        if not self.is_derived:
            return f"no tariff derivable: {self.reason}"
        return (
            f"{self.mode} tariff from {self.n_lanes} of this network's own lanes: "
            f"{self.fixed_leg_cost:,.2f} per unit + {self.rate_per_km:.5f} per unit-km "
            f"(reproduces them to {self.rate_fit_error_pct:.1f}% median); transit "
            f"{self.terminal_time_days:.2f} d + {self.speed_km_per_day:,.0f} km/day "
            f"({self.lead_time_fit_error_pct:.1f}% median)"
        )


def _ols(xs: Sequence[float], ys: Sequence[float]) -> Optional[tuple]:
    """Least-squares (intercept, slope) for y = a + b*x, or None if degenerate."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    denominator = sum((x - mx) ** 2 for x in xs)
    if denominator <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denominator
    return my - slope * mx, slope


def _median_abs_pct_error(xs: Sequence[float], ys: Sequence[float],
                          intercept: float, slope: float) -> float:
    errors = [abs((intercept + slope * x) - y) / y
              for x, y in zip(xs, ys) if y > 0]
    return statistics.median(errors) * 100.0 if errors else 0.0


def _fit_affine_non_negative(xs: Sequence[float], ys: Sequence[float]):
    """
    Fit y = a + b*x with both terms non-negative.

    A negative intercept would mean short hauls are free or refunded, and a
    negative slope that distance reduces cost. Neither is a tariff. Where OLS
    produces one, the offending term is dropped and the remaining one refitted
    through the origin — which is still the client's own data, just a simpler
    reading of it.

    Returns (intercept, slope) or None.
    """
    fit = _ols(xs, ys)
    if fit is None:
        return None
    intercept, slope = fit

    if intercept < 0 or slope <= 0:
        # Proportional fit through the origin: ratio of totals, NOT the mean of
        # per-lane ratios. This is the estimator the previous code should have
        # used at minimum; it weights every lane by its size instead of letting
        # a 9 km leg outvote a 2,000 km one.
        total_x = sum(xs)
        if total_x <= 0:
            return None
        slope = sum(ys) / total_x
        intercept = 0.0
        if slope <= 0:
            return None

    return intercept, slope


def derive_lane_tariff(lanes: Iterable, mode: str = "ROAD") -> LaneTariff:
    """
    Fit this network's freight tariff and transit pace for one transport mode.

    Args:
        lanes: the network's `LaneRecord`s. Only those on `mode` carrying a
               positive distance and a positive rate are used — a lane with no
               distance says nothing about how distance is priced.
        mode:  the transport mode to price, e.g. "ROAD".

    Returns:
        A `LaneTariff`. Check `is_derived` before using it.
    """
    usable: List = []
    for lane in lanes:
        lane_mode = getattr(lane.mode, "value", str(lane.mode))
        if lane_mode != mode:
            continue
        if (lane.distance_km or 0) <= 0 or (lane.rate_per_unit or 0) <= 0:
            continue
        usable.append(lane)

    if len(usable) < MIN_LANES_FOR_FIT:
        return LaneTariff(
            is_derived=False, mode=mode, n_lanes=len(usable),
            reason=(
                f"this network has {len(usable)} {mode} lane(s) with both a "
                f"distance and a rate; at least {MIN_LANES_FOR_FIT} are needed "
                f"to derive what it charges per kilometre"
            ),
        )

    distances = [float(l.distance_km) for l in usable]
    if max(distances) - min(distances) < MIN_DISTANCE_SPREAD_KM:
        return LaneTariff(
            is_derived=False, mode=mode, n_lanes=len(usable),
            reason=(
                f"every {mode} lane in this network is about the same length "
                f"({min(distances):,.0f}-{max(distances):,.0f} km), so its rates "
                f"reveal nothing about how cost varies with distance"
            ),
        )

    rates = [float(l.rate_per_unit) for l in usable]
    rate_fit = _fit_affine_non_negative(distances, rates)
    if rate_fit is None:
        return LaneTariff(
            is_derived=False, mode=mode, n_lanes=len(usable),
            reason=f"the {mode} rates in this network do not vary with distance",
        )
    fixed_leg_cost, rate_per_km = rate_fit

    # Transit is fitted over the same lanes, so the pace quoted for a new lane
    # comes from the same freight the rate does.
    with_lead_time = [(d, float(l.lead_time_days))
                      for d, l in zip(distances, usable)
                      if (l.lead_time_days or 0) > 0]
    terminal_time_days = 0.0
    speed_km_per_day = 0.0
    lead_time_error: Optional[float] = None
    if len(with_lead_time) >= MIN_LANES_FOR_FIT:
        lt_distances = [d for d, _ in with_lead_time]
        lt_days = [t for _, t in with_lead_time]
        lt_fit = _fit_affine_non_negative(lt_distances, lt_days)
        if lt_fit is not None:
            terminal_time_days, per_km_days = lt_fit
            if per_km_days > 0:
                speed_km_per_day = 1.0 / per_km_days
                lead_time_error = _median_abs_pct_error(
                    lt_distances, lt_days, terminal_time_days, per_km_days)

    if speed_km_per_day <= 0:
        # No usable transit evidence. The tariff still prices freight; the
        # caller is told the pace is unknown so it can leave lead time out of
        # the SLA test rather than asserting a speed the network never showed.
        return LaneTariff(
            is_derived=False, mode=mode, n_lanes=len(usable),
            fixed_leg_cost=fixed_leg_cost, rate_per_km=rate_per_km,
            rate_fit_error_pct=_median_abs_pct_error(
                distances, rates, fixed_leg_cost, rate_per_km),
            reason=(
                f"this network's {mode} lanes carry no usable transit times, so "
                f"the days a new lane would take cannot be derived from it"
            ),
        )

    tariff = LaneTariff(
        is_derived=True,
        mode=mode,
        n_lanes=len(usable),
        fixed_leg_cost=fixed_leg_cost,
        rate_per_km=rate_per_km,
        terminal_time_days=terminal_time_days,
        speed_km_per_day=speed_km_per_day,
        rate_fit_error_pct=_median_abs_pct_error(
            distances, rates, fixed_leg_cost, rate_per_km),
        lead_time_fit_error_pct=lead_time_error,
    )
    logger.info("scenarios.tariff.derived %s", tariff.describe())
    return tariff
