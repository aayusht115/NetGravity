"""
NetGravity — What a demand table stating several periods is solved as
=====================================================================
Two things were wrong here, one after the other.

**First, it crashed.** A demand table stating the same market and product for
more than one period produced two MILP constraints with the same name, and PuLP
refused to build the model at all:

    pulp.constants.PulpError: overlapping constraint names: demand_M_P1

Twelve months of demand is the shape most planning data arrives in, so that was
an unexplained solver crash on a perfectly ordinary workbook.

**Then it was collapsed.** The fix for the crash reduced every period to one
representative period and said so loudly. That is honest, and it is still the
right thing to do when someone explicitly asks for a single-period answer — but
it is not a multi-period model. A network that can carry the mean of twelve
months and not the peak of one is a different network, and no averaging policy
can tell you which one you have.

What happens now
----------------
`FULL_HORIZON` (the default) solves every period the data states, as one model:
flow variables carry a period index, demand and capacity bind per period, and
stock may be carried between periods at facilities that can hold it. That is
what makes seasonality answerable — the model can build ahead of a peak instead
of being told the peak does not exist.

The collapse policies remain, because they answer questions of their own:

    FULL_HORIZON         every period modelled, with carryover   (default)
    REPRESENTATIVE_MEAN  the average across periods
    PEAK                 the largest period
    SUM                  every period added together

`PEAK` still answers "can the footprint carry the worst month" against a
single-period model, and is much cheaper to solve. `REPRESENTATIVE_MEAN` is the
cheapest defensible summary. `SUM` is only correct where the periods are meant
to be served simultaneously.

Nothing is silent under any of them. The result carries the periods found, the
policy applied and the per-period totals, so a screen can say what the figures
cover rather than presenting one number as though the data had only ever
described one period.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

#: Policies that reduce the horizon to one period before the model is built.
COLLAPSE_POLICIES = ("REPRESENTATIVE_MEAN", "PEAK", "SUM")

#: Every accepted value of `OptimizationConfig.multi_period_policy`.
POLICIES = ("FULL_HORIZON",) + COLLAPSE_POLICIES

#: What a network with no demand rows at all is solved as.
DEFAULT_PERIOD = 1


def summarise_periods(network: Any) -> Dict[str, Any]:
    """Which periods the demand rows state, and how much each carries."""
    totals: Dict[Any, float] = {}
    for demand in network.demands:
        period = demand.period
        totals[period] = totals.get(period, 0.0) + float(demand.quantity)
    ordered = sorted(totals, key=lambda p: (isinstance(p, str), p))
    quantities = [totals[p] for p in ordered]
    return {
        "periods": ordered,
        "n_periods": len(ordered),
        "total_by_period": {str(p): round(totals[p], 4) for p in ordered},
        "mean_total": round(sum(quantities) / len(quantities), 4) if quantities else 0.0,
        "peak_total": round(max(quantities), 4) if quantities else 0.0,
        "peak_period": (ordered[quantities.index(max(quantities))]
                        if quantities else None),
    }


def collapse_to_representative_period(
        network: Any, policy: str = "REPRESENTATIVE_MEAN") -> Tuple[Any, Dict[str, Any]]:
    """
    Reduce demand to one row per (market, product, sla) under `policy`.

    Returns `(network, report)`. A network already stating one period is
    returned UNCHANGED and its report says so, so the ordinary case pays
    nothing and cannot be perturbed by this code.

    Rows are grouped by market, product AND service level: two rows for the
    same market and product under different SLAs are different commitments, and
    averaging across them would invent a service level the client never stated.
    """
    summary = summarise_periods(network)
    report = {
        **summary,
        "policy": policy,
        "collapsed": False,
        "note": "",
    }
    if summary["n_periods"] <= 1:
        report["note"] = "The data states a single demand period."
        return network, report

    # Checked against the COLLAPSE policies, not every policy: this function
    # cannot express FULL_HORIZON, and quietly accepting the name would return a
    # collapsed network labelled as a full horizon.
    if policy not in COLLAPSE_POLICIES:
        policy = "REPRESENTATIVE_MEAN"
        report["policy"] = policy

    grouped: Dict[Tuple[Any, Any, Any], List[Any]] = {}
    order: List[Tuple[Any, Any, Any]] = []
    for demand in network.demands:
        key = (demand.market_id, demand.product_id, demand.sla_days)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(demand)

    collapsed = []
    for key in order:
        rows = grouped[key]
        quantities = [float(r.quantity) for r in rows]
        if policy == "SUM":
            quantity = sum(quantities)
            source = rows[0]
        elif policy == "PEAK":
            quantity = max(quantities)
            source = rows[quantities.index(quantity)]
        else:
            quantity = sum(quantities) / len(quantities)
            source = rows[0]

        # Variability travels with the quantity it describes. Averaging a
        # standard deviation is not exact, but leaving the first period's
        # against an averaged quantity would be plainly wrong, and the
        # inventory module reads it.
        deviations = [float(getattr(r, "std_dev", 0.0) or 0.0) for r in rows]
        std_dev = (max(deviations) if policy == "PEAK"
                   else sum(deviations) / len(deviations))

        collapsed.append(source.model_copy(update={
            "quantity": quantity,
            "std_dev": std_dev,
            # One representative period. Keeping the source row's index would
            # imply the figure describes that period specifically.
            "period": 1,
        }))

    report["collapsed"] = True
    report["modelled_periods"] = 1
    report["rows_before"] = len(network.demands)
    report["rows_after"] = len(collapsed)
    report["note"] = (
        f"This data states {summary['n_periods']} demand periods. "
        f"multi_period_policy={policy} was requested, so demand was collapsed to "
        f"one representative period rather than the horizon being modelled: "
        + {
            "REPRESENTATIVE_MEAN": (
                f"the mean of {summary['n_periods']} periods "
                f"({summary['mean_total']:,.0f} units). The peak period "
                f"({summary['peak_period']}) carries {summary['peak_total']:,.0f} "
                f"units, which this solve does not size for. "
                f"multi_period_policy=FULL_HORIZON models every period instead."),
            "PEAK": (
                f"the largest period ({summary['peak_period']}, "
                f"{summary['peak_total']:,.0f} units). Cost is therefore the "
                f"cost of the worst period, not the average one."),
            "SUM": (
                f"every period added together ({sum(summary['total_by_period'].values()):,.0f} "
                f"units) against per-period capacity, which is only correct if "
                f"the periods are meant to be served simultaneously."),
        }[policy]
    )

    logger.info(
        "optimization.periods.collapsed network_id=%s periods=%d policy=%s "
        "rows=%d->%d",
        getattr(network, "network_id", "?"), summary["n_periods"], policy,
        len(network.demands), len(collapsed),
    )
    return network.model_copy(update={"demands": collapsed}), report


def resolve_horizon(
        network: Any, policy: str = "FULL_HORIZON") -> Tuple[Any, List[Any], Dict[str, Any]]:
    """
    Decide which periods the MILP will actually carry variables for.

    Returns `(network, periods, report)`.

    * A network stating one period returns that period and an untouched
      network, so the ordinary case pays nothing and cannot be perturbed here.
    * A collapse policy returns the collapsed network and `[1]`.
    * `FULL_HORIZON` returns the network unchanged and every period in it, in
      order. The MILP indexes its flow variables, demand balance, capacity and
      stock by that list.

    The period labels are returned as they appear in the data rather than
    renumbered, so a constraint name, a warning and a `FlowDecision.period` all
    say the same thing the demand row said.
    """
    summary = summarise_periods(network)
    periods = list(summary["periods"]) or [DEFAULT_PERIOD]

    if summary["n_periods"] <= 1:
        return network, periods, {
            **summary,
            "policy": policy,
            "collapsed": False,
            "modelled_periods": 1,
            "note": "The data states a single demand period.",
        }

    if policy in COLLAPSE_POLICIES:
        collapsed_network, report = collapse_to_representative_period(network, policy)
        return collapsed_network, [DEFAULT_PERIOD], report

    # FULL_HORIZON, and anything unrecognised. Modelling every period the data
    # states is the answer that assumes least about what the caller meant; an
    # unknown name silently collapsing a horizon would be the damaging default.
    report = {
        **summary,
        "policy": "FULL_HORIZON",
        "collapsed": False,
        "modelled_periods": len(periods),
        "note": (
            f"This data states {summary['n_periods']} demand periods and all "
            f"{summary['n_periods']} are modelled. Demand and capacity bind "
            f"period by period, and stock may be carried between periods where "
            f"a facility can hold it. Costs shown are horizon totals over "
            f"{summary['n_periods']} periods, not one period: fixed and "
            f"handling costs are charged in every period a facility is open, "
            f"while opening, closure and capex are charged once. The peak "
            f"period ({summary['peak_period']}) carries "
            f"{summary['peak_total']:,.0f} units against a mean of "
            f"{summary['mean_total']:,.0f}."
        ),
    }
    if policy != "FULL_HORIZON":
        report["note"] = (
            f"multi_period_policy={policy!r} is not a policy this model knows, "
            f"so the horizon was modelled in full rather than reduced by a rule "
            f"nobody chose. " + report["note"]
        )
    logger.info(
        "optimization.periods.full_horizon network_id=%s periods=%d",
        getattr(network, "network_id", "?"), len(periods),
    )
    return network, periods, report
