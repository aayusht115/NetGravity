"""
NetGravity — Evidence for one KPI chart
=======================================
What a chart's explanation is allowed to talk about, and what it must lead with.

WHY THIS IS FOUR BUILDERS AND NOT ONE
-------------------------------------
One generic "here are the numbers, explain them" payload produces four
explanations that all read the same, because the model is left to decide what
the point is — and the point is different for every chart:

  * peak vs average      the finding is the PAIR: a site whose average sits
                         under the threshold and whose peak does not has no
                         room in the month that decides whether it works;
  * capacity carried     the finding is the INVERSION: this chart ranks by
                         units and the one above it ranks by per cent, and
                         95% of 200 is ten spare while 80% of 40,000 is eight
                         thousand;
  * stock held           the finding is the GAP between a site's two bars —
                         a seasonal build and a flat buffer are different
                         operations and look identical in the average;
  * throughput horizon   the finding is the TREND against the ceiling.

So each builder computes its own lead — in code — and hands it over already
decided. The model writes the sentences; it does not choose the subject.

NO FIGURE HERE IS WRITTEN BY A MODEL
------------------------------------
`card.py` states the rule: the model writes no numbers at all, and code
produces `figures`. Every value below comes from the backend's own
`WarehouseHealthKPI` records — the same rows the chart drew — so a figure in
the explanation and a bar on the chart cannot disagree.

ABSENCE IS NOT ZERO
-------------------
A reading the solve did not produce stays `None` and is counted as absent. A
site with no stock reading is not a site holding none, and a payload that
turned one into 0 would have the model explain a measurement nobody made.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from netgravity.orchestrator.reasoning.card import Figure

#: The charts a reader can ask about. One per visual that takes more than a
#: glance to read — the ranked lists and the share-of-total donuts are not
#: here, because a sorted list with its values on it explains itself and a
#: button offering to explain it teaches the reader that the button means
#: nothing.
CHART_PEAK_VS_AVERAGE = "peak_vs_average"
CHART_CAPACITY_CARRIED = "capacity_carried"
CHART_STOCK_HELD = "stock_held"
CHART_THROUGHPUT_HORIZON = "throughput_horizon"

CHARTS = (CHART_PEAK_VS_AVERAGE, CHART_CAPACITY_CARRIED,
          CHART_STOCK_HELD, CHART_THROUGHPUT_HORIZON)

#: Charts about the population on screen, rather than about one site.
NETWORK_CHARTS = (CHART_PEAK_VS_AVERAGE, CHART_CAPACITY_CARRIED,
                  CHART_STOCK_HELD)

#: A peak at or above this share of a site's own average is a seasonal build
#: rather than a buffer held all year. Two readings of the same two bars, and
#: the whole reason the chart draws both.
SEASONAL_RATIO = 1.5
FLAT_RATIO = 1.2


def _sentences(chart: str, finding: str, matters: str = "") -> Dict[str, Any]:
    """
    The deterministic reading of this chart, written from its own numbers.

    WHY THIS EXISTS. With no model credential — the default, and the state of
    every test run — the shared template writer produces prose only for the
    payload blocks it recognises, and it does not recognise a chart. Without
    this it emitted "I could not find a deterministic result to explain",
    which is a worse card than no card: the button promises a briefing and
    delivers an apology.

    So the chart writes its own reading here, beside the numbers it is about,
    and `reasoning_agent` surfaces it through one generic branch. A model, when
    one is configured, writes better sentences over the same evidence; this is
    the floor, not the ceiling.

    THERE IS NO NEXT STEP, deliberately.

    A chart explanation says what the chart SHOWS. Telling a reader to close a
    site or test a scenario is a different job with a different burden of
    proof: it needs closure economics, contractual constraints and a second
    solve, none of which a utilisation chart has. Those recommendations belong
    to the Overview and the Scenario Planner, which have them. A card that
    describes and a card that prescribes are two products, and mixing them
    puts an unsupported instruction under a supported observation.
    """
    return {"kpi_chart": {"chart": chart, "finding": finding,
                          "matters": matters}}


def _num(value: Any) -> Optional[float]:
    """A number, or absence. Never a substituted zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None   # NaN is absence, not a value


def _name(row: Any) -> str:
    return str(getattr(row, "facility_name", None)
               or getattr(row, "facility_id", None) or "")


def _get(row: Any, field: str) -> Any:
    return getattr(row, field, None)


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _units(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.0f}"


# ─── Peak against average ───────────────────────────────────

def _peak_vs_average(rows: Sequence[Any], *, threshold_pct: float,
                     multi_period: bool) -> Tuple[Dict[str, Any], List[Figure]]:
    sites: List[Dict[str, Any]] = []
    for row in rows:
        avg = _num(_get(row, "avg_utilization_pct"))
        peak = _num(_get(row, "peak_utilization_pct"))
        sites.append({
            "facility": _name(row),
            "avg_utilization_pct": avg,
            "peak_utilization_pct": peak,
            #: The distance between the two bars, which is the picture.
            "peak_above_average_pts": (None if avg is None or peak is None
                                       else round(peak - avg, 2)),
            "peak_period": _get(row, "peak_period"),
            "tight_periods": _get(row, "bottleneck_periods_count"),
            "periods_observed": _get(row, "periods_observed"),
        })

    # THE LEAD. Sites the average declares fine and the peak does not — the
    # exact reading a single averaged bar would have hidden, and the reason
    # this chart draws two.
    hidden = [s for s in sites
              if s["avg_utilization_pct"] is not None
              and s["peak_utilization_pct"] is not None
              and s["avg_utilization_pct"] < threshold_pct <= s["peak_utilization_pct"]]

    # Tight in every period observed: not a seasonal peak at all, but a site
    # that has no room at any point in the horizon.
    persistent = [s for s in sites
                  if s["tight_periods"] and s["periods_observed"]
                  and s["tight_periods"] == s["periods_observed"]]

    over = [s for s in sites if (s["peak_utilization_pct"] or 0) >= threshold_pct]
    widest = max((s for s in sites if s["peak_above_average_pts"] is not None),
                 key=lambda s: s["peak_above_average_pts"], default=None)

    names = lambda rows_: ", ".join(s["facility"] for s in rows_[:3]) + (
        f" and {len(rows_) - 3} more" if len(rows_) > 3 else "")

    if hidden:
        finding = (f"{names(hidden)} "
                   f"{'sits' if len(hidden) == 1 else 'sit'} below the threshold on "
                   f"average and above it in the busiest period.")
        matters = ("The average is the reading that hides this, which is why both "
                   "bars are drawn. There is no room at these sites in the period "
                   "that decides whether the plan works.")

    elif over:
        finding = (f"{names(over)} reach the threshold in the busiest "
                   f"period.")
        matters = ("Each is already read as tight by its average, so the peak "
                   "confirms rather than reveals the constraint.")

    else:
        finding = ("No site reaches the utilisation threshold, in its busiest "
                   "period or on average.")
        matters = "Capacity is not what limits this part of the plan."

    if persistent:
        matters += (f" {names(persistent)} "
                    f"{'is' if len(persistent) == 1 else 'are'} at the threshold in "
                    f"every period observed, which is not a seasonal peak but a "
                    f"site with no room at any point in the horizon.")

    payload = {
        **_sentences(CHART_PEAK_VS_AVERAGE, finding, matters),
        "utilisation_peak_vs_average": {
            "threshold_pct": threshold_pct,
            "multi_period": multi_period,
            "n_sites": len(sites),
            "n_at_or_above_threshold_in_peak": len(over),
            #: Named so the briefing leads with it.
            "n_below_threshold_on_average_but_not_in_peak": len(hidden),
            "hidden_by_the_average": [s["facility"] for s in hidden],
            "n_tight_in_every_period": len(persistent),
            "tight_in_every_period": [s["facility"] for s in persistent],
            "widest_peak_above_average": (
                None if widest is None
                else {"facility": widest["facility"],
                      "points": widest["peak_above_average_pts"]}),
        },
        "utilisation_sites": sites,
    }

    figures = [
        Figure(label=f"At or above {threshold_pct:.0f}% in their peak period",
               value=f"{len(over)} of {len(sites)}"),
        Figure(label="Fine on average, tight in the peak",
               value=str(len(hidden)),
               note="the reading an average hides" if hidden else "none"),
    ]
    if widest is not None:
        figures.append(Figure(
            label="Widest gap between peak and average",
            value=f"{widest['peak_above_average_pts']:.1f} pts",
            note=widest["facility"]))
    return payload, figures


# ─── Capacity, and what the busiest period puts in it ───────

def _capacity_carried(rows: Sequence[Any], *,
                      multi_period: bool) -> Tuple[Dict[str, Any], List[Figure]]:
    sites: List[Dict[str, Any]] = []
    for row in rows:
        capacity = _num(_get(row, "rated_capacity_per_period"))
        used = _num(_get(row, "peak_throughput_units"))
        peak_pct = _num(_get(row, "peak_utilization_pct"))
        sites.append({
            "facility": _name(row),
            "rated_capacity_per_period": capacity,
            "used_in_peak_period": used,
            "headroom_units": (None if capacity is None or used is None
                               else round(capacity - used, 2)),
            "peak_utilization_pct": peak_pct,
        })

    measured = [s for s in sites if s["headroom_units"] is not None]
    by_units = sorted(measured, key=lambda s: -s["headroom_units"])
    by_percent = sorted((s for s in measured if s["peak_utilization_pct"] is not None),
                        key=lambda s: s["peak_utilization_pct"])

    # THE LEAD. Where the next volume can actually go is a question about
    # UNITS, and the per-cent chart above this one ranks the answer the wrong
    # way round. This states whether the two rankings disagree, which is the
    # only reason to draw this chart at all.
    most_room = by_units[0] if by_units else None
    emptiest = by_percent[0] if by_percent else None
    inverted = bool(most_room and emptiest
                    and most_room["facility"] != emptiest["facility"])

    if most_room is None:
        finding = "No site on this chart states both a capacity and what it carries."
        matters = ""
    elif inverted:
        finding = (f"{most_room['facility']} has the most room for more volume in "
                   f"units, despite not being the emptiest site by percentage.")
        matters = (f"{emptiest['facility']} looks emptiest on a percentage reading, but "
                   f"holds far less actual room. Where the next volume can go is a "
                   f"question about units, and the percentage chart above ranks it the "
                   f"other way round.")

    else:
        finding = (f"{most_room['facility']} has both the most room in units and "
                   f"the lowest utilisation on this chart.")
        matters = ("Both readings agree here, so the percentage chart tells the "
                   "same story this one does.")

    payload = {
        **_sentences(CHART_CAPACITY_CARRIED, finding, matters),
        "capacity_headroom": {
            "multi_period": multi_period,
            "n_sites": len(sites),
            "total_headroom_units": (round(sum(s["headroom_units"] for s in measured), 2)
                                     if measured else None),
            "most_headroom_in_units": (
                None if most_room is None
                else {"facility": most_room["facility"],
                      "headroom_units": most_room["headroom_units"],
                      "peak_utilization_pct": most_room["peak_utilization_pct"]}),
            "emptiest_by_percent": (
                None if emptiest is None
                else {"facility": emptiest["facility"],
                      "peak_utilization_pct": emptiest["peak_utilization_pct"],
                      "headroom_units": emptiest["headroom_units"]}),
            #: True when the site with the most ROOM is not the site that
            #: looks emptiest — the finding this chart exists to show.
            "unit_and_percent_rankings_disagree": inverted,
        },
        "capacity_sites": sites,
    }

    figures: List[Figure] = []
    if most_room is not None:
        figures.append(Figure(
            label="Most room for more volume",
            value=f"{_units(most_room['headroom_units'])} units",
            note=most_room["facility"]))
    if emptiest is not None and inverted:
        figures.append(Figure(
            label="Emptiest by percentage",
            value=_pct(emptiest["peak_utilization_pct"]),
            note=f"{emptiest['facility']} — {_units(emptiest['headroom_units'])} units"))
    if measured:
        figures.append(Figure(
            label="Headroom across the sites shown",
            value=f"{_units(sum(s['headroom_units'] for s in measured))} units"))
    return payload, figures


# ─── Stock held ─────────────────────────────────────────────

def _stock_held(rows: Sequence[Any]) -> Tuple[Dict[str, Any], List[Figure]]:
    sites: List[Dict[str, Any]] = []
    for row in rows:
        avg = _num(_get(row, "avg_inventory_units"))
        peak = _num(_get(row, "peak_inventory_units"))
        ratio = (None if not avg or peak is None else round(peak / avg, 3))
        sites.append({
            "facility": _name(row),
            "avg_inventory_units": avg,
            "peak_inventory_units": peak,
            "peak_above_average_units": (None if avg is None or peak is None
                                         else round(peak - avg, 2)),
            "peak_to_average_ratio": ratio,
        })

    held = [s for s in sites if s["peak_to_average_ratio"] is not None]
    # THE LEAD. The gap between a site's two bars is the difference between
    # building for a season and holding a buffer all year — two different
    # operations that an average reports identically.
    seasonal = [s for s in held if s["peak_to_average_ratio"] >= SEASONAL_RATIO]
    flat = [s for s in held if s["peak_to_average_ratio"] <= FLAT_RATIO]
    widest = max(held, key=lambda s: s["peak_to_average_ratio"], default=None)

    listed = lambda rows_: ", ".join(s["facility"] for s in rows_[:3]) + (
        f" and {len(rows_) - 3} more" if len(rows_) > 3 else "")

    if seasonal:
        finding = (f"{listed(seasonal)} "
                   f"{'builds' if len(seasonal) == 1 else 'build'} stock for a season "
                   f"rather than holding a buffer.")
        matters = ("A peak well above a site's own average is a seasonal build; a peak "
                   "close to it is a buffer held all year. The two are different "
                   "operations and an average reports them identically.")
    elif flat:
        finding = (f"Every site reporting stock here holds a broadly constant "
                   f"buffer: {listed(flat)}.")
        matters = ("None of them shows the seasonal swing this chart is drawn to "
                   "reveal, so stock is not following demand at these sites.")
    else:
        finding = "The sites reporting stock sit between a flat buffer and a seasonal build."
        matters = ""

    if len(sites) - len(held):
        matters += (" Some sites report no stock level at all, which is a model that "
                    "does not carry stock rather than a site holding none.")

    payload = {
        **_sentences(CHART_STOCK_HELD, finding, matters),
        "stock_profile": {
            "n_sites": len(sites),
            "n_reporting_stock": len(held),
            "n_not_reporting_stock": len(sites) - len(held),
            "seasonal_ratio_threshold": SEASONAL_RATIO,
            "flat_ratio_threshold": FLAT_RATIO,
            "n_building_for_a_season": len(seasonal),
            "building_for_a_season": [s["facility"] for s in seasonal],
            "n_holding_a_flat_buffer": len(flat),
            "holding_a_flat_buffer": [s["facility"] for s in flat],
            "largest_seasonal_swing": (
                None if widest is None
                else {"facility": widest["facility"],
                      "peak_to_average_ratio": widest["peak_to_average_ratio"],
                      "peak_above_average_units": widest["peak_above_average_units"]}),
        },
        "stock_sites": sites,
    }

    figures = [
        Figure(label="Sites building for a season",
               value=str(len(seasonal)),
               note=f"peak at least {SEASONAL_RATIO:g}x their average"),
        Figure(label="Sites holding a flat buffer", value=str(len(flat))),
    ]
    if widest is not None:
        figures.append(Figure(
            label="Largest swing between average and peak",
            value=f"{widest['peak_to_average_ratio']:.2f}x",
            note=widest["facility"]))
    return payload, figures


# ─── One site against its ceiling ───────────────────────────

def _throughput_horizon(row: Any, *,
                        multi_period: bool) -> Tuple[Dict[str, Any], List[Figure]]:
    capacity = _num(_get(row, "rated_capacity_per_period"))
    avg = _num(_get(row, "avg_throughput_units"))
    peak = _num(_get(row, "peak_throughput_units"))
    avg_pct = _num(_get(row, "avg_utilization_pct"))
    peak_pct = _num(_get(row, "peak_utilization_pct"))
    tight = _get(row, "bottleneck_periods_count")
    observed = _get(row, "periods_observed")

    headroom = (None if capacity is None or peak is None else capacity - peak)
    name = _name(row)
    if peak_pct is None:
        finding = f"{name} reports no utilisation for this horizon."
        matters = ""
    else:
        finding = (f"{name} runs closer to its capacity in the busiest period than "
                   f"its average across the horizon suggests."
                   if avg_pct is not None and peak_pct > avg_pct else
                   f"{name} carries a steady load across the horizon.")
        matters = ("The period that decides whether a site works is its busiest one, "
                   "and the average across the horizon does not show it."
                   if headroom is not None else
                   "The busiest period is the one that decides whether it works.")
        if tight and observed:
            matters += (" It reaches the threshold in more than one period, so this is "
                        "not a single seasonal spike.")


    payload = {
        **_sentences(CHART_THROUGHPUT_HORIZON, finding, matters),
        "facility_throughput": {
            "facility": _name(row),
            "multi_period": multi_period,
            "rated_capacity_per_period": capacity,
            "avg_throughput_units": avg,
            "peak_throughput_units": peak,
            "avg_utilization_pct": avg_pct,
            "peak_utilization_pct": peak_pct,
            "peak_period": _get(row, "peak_period"),
            "tight_periods": tight,
            "periods_observed": observed,
            #: What is left in the busiest period — the number that decides
            #: whether this site can absorb anything more.
            "headroom_in_peak_period_units": (None if capacity is None or peak is None
                                              else round(capacity - peak, 2)),
            "health_band": _get(row, "health_band"),
            "is_open": _get(row, "is_open"),
        },
    }

    figures = [
        Figure(label="Average utilisation", value=_pct(avg_pct)),
        Figure(label="Peak utilisation", value=_pct(peak_pct),
               note=str(_get(row, "peak_period") or "")),
    ]
    if tight is not None and observed:
        figures.append(Figure(label="Periods at or above the threshold",
                              value=f"{tight} of {observed}"))
    return payload, figures


# ─── The one entry point ────────────────────────────────────

def chart_payload(chart: str, rows: Sequence[Any], *,
                  threshold_pct: float = 90.0,
                  multi_period: bool = True,
                  facility_id: Optional[str] = None
                  ) -> Tuple[Dict[str, Any], List[Figure]]:
    """
    The evidence for one chart, and the figures its card will carry.

    `rows` are `WarehouseHealthKPI` records — EXACTLY the ones the chart drew,
    filtered the way the screen was filtered. An explanation built over the
    whole network while the reader is looking at three southern sites would
    describe sites that are not on the screen, which is worse than no
    explanation: it is a confident one about the wrong thing.

    Returns `({}, [])` for a chart with nothing to describe, so the caller can
    say there is nothing to explain rather than spending a request to be told
    so.
    """
    rows = [r for r in rows if r is not None]

    if chart == CHART_THROUGHPUT_HORIZON:
        row = next((r for r in rows if _get(r, "facility_id") == facility_id), None)
        if row is None:
            return {}, []
        # THE CHART IS THE SERIES, so no series is nothing to explain — the
        # same guard the stock chart applies below, one screen over.
        #
        # This builder reads the peak, the average and the tight-period count,
        # every one of which survives on a row whose per-period series is
        # empty. So it wrote a horizon finding for a card that had drawn a
        # sentence saying there was no horizon to draw. The figures were
        # right and the subject did not exist.
        #
        # Fewer than two points is the chart's own test for a horizon: one
        # period is not a trend, and a single-period solve carries no series
        # by design.
        series = _get(row, "throughput_by_period") or {}
        if len(series) < 2:
            return {}, []
        return _throughput_horizon(row, multi_period=multi_period)

    if not rows:
        return {}, []

    if chart == CHART_PEAK_VS_AVERAGE:
        return _peak_vs_average(rows, threshold_pct=threshold_pct,
                                multi_period=multi_period)
    if chart == CHART_CAPACITY_CARRIED:
        return _capacity_carried(rows, multi_period=multi_period)
    if chart == CHART_STOCK_HELD:
        # Nothing to explain where no site reports a stock level. The card on
        # screen already states the engine's reason for that, and a briefing
        # here would be a second, vaguer copy of it.
        if not any(_num(_get(r, "peak_inventory_units")) is not None for r in rows):
            return {}, []
        return _stock_held(rows)

    return {}, []
