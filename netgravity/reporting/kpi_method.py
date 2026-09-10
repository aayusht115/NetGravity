"""
NetGravity — how the KPI screen's figures are calculated
========================================================
The document a senior reader asks for when they are about to repeat one of
these numbers to somebody who will challenge it.

WHAT MAKES IT CONVINCING, AND WHAT WOULD NOT
--------------------------------------------
Not prose about rigour. The three things a challenged figure needs are the
RULE it was computed by, the VALUES that went into the rule, and the same rule
with those values substituted so the arithmetic can be followed to the number
on the screen. Every metric below carries all three, and the substitution is
built from the very figures the screen drew — not recomputed here, because a
document whose worked example disagrees with its own results table is worse
than one with no example at all.

WHAT THIS MODULE DOES NOT DO
----------------------------
It computes no KPI. Every value it prints is read from the solved records —
the authoritative KPI layer for the network figures, `WarehouseHealthKPI` rows
for the per-site ones. The arithmetic shown is the arithmetic the engine did;
this states it, it does not repeat it. Where a reading is absent the equation
is still shown and the substitution says so, because "we cannot compute this
and here is why" is the honest form of an answer a reader will otherwise
assume was zero.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from netgravity.reporting.derivation import (
    DerivationReport,
    DerivationStep,
    Equation,
    Figure,
    Variable,
)

#: The engine that owns each family of figures, for the "From" column.
_MILP = "MILP solve"
_UPLOAD = "Your upload"
_POLICY = "Threshold policy"


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _kpi(kpis: Dict[str, Any], metric_id: str) -> Optional[float]:
    """One authoritative figure, and only when the layer marked it VALID."""
    row = (kpis or {}).get(metric_id)
    if not isinstance(row, dict):
        return _num(row)
    if str(row.get("status") or "").upper() != "VALID":
        return None
    return _num(row.get("value"))


def _display(kpis: Dict[str, Any], metric_id: str) -> str:
    """The engine's own rendering of a figure, or an honest absence."""
    row = (kpis or {}).get(metric_id)
    if isinstance(row, dict):
        if str(row.get("status") or "").upper() != "VALID":
            return "Not available"
        text = row.get("display_value")
        if text:
            return str(text)
        value = _num(row.get("value"))
        return _plain(value)
    value = _num(row)
    return _plain(value)


def _plain(value: Optional[float]) -> str:
    """
    A bare figure at the precision a reader of a board document reads at.

    Whole units above the rate threshold, two decimals below it — the same
    rule `format_money` applies on the evidence layer, so a figure does not
    change shape between the screen and the document explaining the screen.
    """
    if value is None:
        return "Not available"
    return f"{value:,.2f}" if abs(value) < _RATE_THRESHOLD else f"{value:,.0f}"


def _units(value: Optional[float], suffix: str = "") -> str:
    if value is None:
        return "Not available"
    return f"{value:,.0f}{suffix}"


def _pct(value: Optional[float]) -> str:
    return "Not available" if value is None else f"{value:,.1f}%"


#: Below this an amount is a RATE and its decimals are the measurement; at or
#: above it they are solver residue. Mirrors `_RATE_THRESHOLD` in
#: netgravity/orchestrator/reasoning/evidence.py.
_RATE_THRESHOLD = 100.0


def _money(value: Optional[float], currency: str) -> str:
    """
    An amount, without the cents.

    This document is read by the people who sign off what it recommends, and
    "INR 150,627.70" claims a precision the model does not have: the seventy
    paise are solver residue on a figure that is the output of a relaxation.
    Rates keep their decimals — see `_RATE_THRESHOLD` — because at that scale
    the decimals ARE the quantity.
    """
    if value is None:
        return "Not available"
    prefix = f"{currency} " if currency else ""
    places = 2 if abs(value) < _RATE_THRESHOLD else 0
    sign = "-" if value < 0 else ""
    return f"{sign}{prefix}{abs(value):,.{places}f}"


# ─────────────────────────────────────────────────────────────
# The steps
# ─────────────────────────────────────────────────────────────

def _cost_step(kpis: Dict[str, Any], currency: str) -> DerivationStep:
    """What the network costs, and what the total is the sum of."""
    parts = [
        ("transport_cost", "Transport", "moving every unit along the corridors "
                                        "the plan routes it on"),
        ("facility_cost", "Facility", "the fixed cost of every site the plan "
                                      "keeps open, over the horizon"),
        ("handling_cost", "Handling", "the per-unit cost of putting volume "
                                      "through each site"),
        ("inventory_cost", "Inventory", "holding stock, decided for the "
                                        "network rather than per site"),
        ("opening_cost", "Opening", "one-off cost of any site the plan opens"),
        ("closure_cost", "Closure", "one-off cost of any site the plan closes"),
    ]
    present = [(mid, label, gloss) for mid, label, gloss in parts
               if _kpi(kpis, mid) is not None]
    total = _kpi(kpis, "business_network_cost")

    variables = [
        Variable(label, gloss, _money(_kpi(kpis, mid), currency), _MILP)
        for mid, label, gloss in present
    ]
    summed = " + ".join(label for _, label, _ in present) or "the components"
    worked = " + ".join(
        f"{_kpi(kpis, mid):,.0f}" for mid, _, _ in present
        if _kpi(kpis, mid) is not None)

    equation = Equation(
        name="Total network cost",
        formula=f"C_network = {summed}",
        variables=variables,
        substituted=(f"C_network = {worked} = {_money(total, currency)}"
                     if worked and total is not None
                     else "C_network = Not available"),
        note=("The shortage penalty the solver uses to decide which demand to "
              "strand is deliberately excluded: nobody pays it. Demand the "
              "plan does not serve is reported as a quantity, never as money."),
    )
    return DerivationStep(
        title="What the network costs",
        detail=("The objective the optimiser minimises. Every component is a "
                "solved quantity multiplied by a rate your upload stated — no "
                "component is estimated, and the total is their sum and "
                "nothing else."),
        figures=[Figure("Total network cost",
                        _display(kpis, "business_network_cost"),
                        "Measured", _MILP)]
                + [Figure(label, _money(_kpi(kpis, mid), currency),
                          "Component", _MILP)
                   for mid, label, _ in present],
        equations=[equation],
    )


def _utilisation_step(rows: Sequence[Any], threshold: float) -> DerivationStep:
    """How a site's utilisation is computed and how its band is set."""
    example = next((k for k in rows
                    if _num(getattr(k, "peak_utilization_pct", None)) is not None
                    and _num(getattr(k, "rated_capacity_per_period", None))),
                   None)

    if example is not None:
        name = (getattr(example, "facility_name", None)
                or getattr(example, "facility_id", ""))
        peak_tp = _num(getattr(example, "peak_throughput_units", None))
        rated = _num(getattr(example, "rated_capacity_per_period", None))
        peak_u = _num(getattr(example, "peak_utilization_pct", None))
        variables = [
            Variable("T_peak", "Units through the site in its busiest period",
                     _units(peak_tp), _MILP),
            Variable("C_rated", "Rated capacity for one period",
                     _units(rated), _UPLOAD),
        ]
        substituted = (f"U_peak = ({peak_tp:,.0f} / {rated:,.0f}) x 100 "
                       f"= {peak_u:,.1f}%"
                       if None not in (peak_tp, rated, peak_u) and rated
                       else "U_peak = Not available")
        worked_for = f"Worked through for {name}."
    else:
        variables = [
            Variable("T_peak", "Units through the site in its busiest period",
                     "Not available", _MILP),
            Variable("C_rated", "Rated capacity for one period",
                     "Not available", _UPLOAD),
        ]
        substituted = "U_peak = Not available"
        worked_for = ("No site in this plan reports both a throughput and a "
                      "rated capacity, so there is no worked example to show.")

    utilisation = Equation(
        name="Peak utilisation of one site",
        formula="U_peak = (T_peak / C_rated) x 100",
        variables=variables,
        substituted=substituted,
        note=(worked_for + " PEAK, not average, and that is the whole point: "
              "a site that fits on average and does not fit in its busiest "
              "period has no room in the month that decides whether the plan "
              "works."),
    )

    banding = Equation(
        name="The band that colours it",
        formula=("CRITICAL if U_peak >= 100 ; "
                 "TIGHT if U_peak >= T_over ; "
                 "UNDERUSED if U_peak <= T_under ; "
                 "otherwise HEALTHY"),
        variables=[
            Variable("T_over", "Utilisation at or above which a site is tight",
                     _pct(threshold), _POLICY),
            Variable("U_peak", "The figure computed above", "per site", _MILP),
        ],
        note=("A site the plan does not open is NOT_OPERATING rather than "
              "healthy at 0%: a closed site has no utilisation to be healthy "
              "about. The thresholds are policy configuration, identical for "
              "every project, not a judgement made about this network."),
    )

    headroom = Equation(
        name="Headroom, in units",
        formula="H = C_rated - T_peak",
        variables=[
            Variable("C_rated", "Rated capacity for one period",
                     "per site", _UPLOAD),
            Variable("T_peak", "Units in the busiest period", "per site", _MILP),
        ],
        note=("Reported in UNITS as well as per cent because the two rank "
              "sites differently, and the difference decides where volume can "
              "actually go: 95% of 200 units is ten units spare, 80% of "
              "40,000 is eight thousand."),
    )

    return DerivationStep(
        title="How hard each site is working",
        detail=("Utilisation is computed per site per period and then read at "
                "its peak across the horizon. The band beside it is a "
                "classification of that one figure against configured "
                "thresholds."),
        figures=[
            Figure("Sites in this view", str(len(rows)), "Count", _MILP),
            Figure("Tight threshold", _pct(threshold), "Policy", _POLICY),
        ],
        equations=[utilisation, banding, headroom],
    )


def _service_step(kpis: Dict[str, Any]) -> DerivationStep:
    """Whether the plan actually serves the demand it was given."""
    served = _kpi(kpis, "served_demand")
    total = _kpi(kpis, "total_demand")
    fill = _kpi(kpis, "demand_fill_rate")

    # The layer reports a fill rate as a RATIO on some networks and a
    # percentage on others; the display string is authoritative either way.
    substituted = (f"F = ({served:,.0f} / {total:,.0f}) x 100 = "
                   f"{(served / total) * 100:,.1f}%"
                   if None not in (served, total) and total
                   else "F = Not available")

    equation = Equation(
        name="Demand fill rate",
        formula="F = (D_served / D_total) x 100",
        variables=[
            Variable("D_served", "Units the plan delivers", _units(served), _MILP),
            Variable("D_total", "Units of demand stated in your upload",
                     _units(total), _UPLOAD),
        ],
        substituted=substituted,
        note=("Demand the plan does not serve is reported as a quantity, not "
              "as a cost. The solver uses an internal shortage penalty to "
              "decide WHICH demand to strand when it cannot serve all of it; "
              "that penalty is a decision device and is excluded from every "
              "cost figure in this document, because nobody pays it."),
    )
    return DerivationStep(
        title="Whether the plan serves the demand",
        detail=("One figure, and the one that outranks cost: a plan that "
                "costs less while stranding demand is cheaper and not "
                "therefore better."),
        figures=[
            Figure("Demand fill rate", _display(kpis, "demand_fill_rate"),
                   "Measured", _MILP),
            Figure("Total demand", _units(total), "Input", _UPLOAD),
            Figure("Served demand", _units(served), "Measured", _MILP),
            Figure("Unserved demand", _units(
                None if None in (served, total) else total - served),
                "Derived", _MILP),
        ],
        equations=[equation],
    )


def _fixed_cost_step(currency: str) -> DerivationStep:
    """The period normalisation, because it is the one nobody expects."""
    equation = Equation(
        name="A facility's fixed cost over the horizon",
        formula="C_fixed = (F_annual / 12) x N_periods",
        variables=[
            Variable("F_annual", "The site's fixed cost for a year",
                     "per site", _UPLOAD),
            Variable("N_periods", "Periods the plan was solved over",
                     "per network", _UPLOAD),
        ],
        note=("WHY THIS IS STATED SEPARATELY. A workbook may quote a fixed "
              "cost per month or per year, and the two differ by a factor of "
              "twelve. NetGravity reads the period from the column's own "
              "heading — a column named for a year is used as a year, one "
              "named for a month is annualised — and states which reading it "
              "took in the assumptions recorded against your upload. On a "
              "single-period plan the distinction lands whole: a year's fixed "
              "cost charged as one month's would be twelve times the truth, "
              "and would make opening one site look like it doubled the cost "
              "of the network."),
    )
    return DerivationStep(
        title="How a fixed cost becomes a cost for this horizon",
        detail=("Fixed costs are stated for a period; the plan covers a "
                "horizon. This is the conversion between them."),
        equations=[equation],
    )


def build_kpi_method_report(*, kpis: Dict[str, Any], report: Any,
                            scope: Dict[str, Any], threshold: float,
                            currency: str = "") -> DerivationReport:
    """
    The methodology behind the KPI screen, as a derivation report.

    `report` is the warehouse deep dive already narrowed to what the screen
    was showing, so a worked example is drawn from a site the reader can see.
    """
    rows = list(getattr(report, "health_kpis", None) or [])

    steps: List[DerivationStep] = [
        _cost_step(kpis, currency),
        _utilisation_step(rows, threshold),
        _service_step(kpis),
        _fixed_cost_step(currency),
    ]

    lens = scope.get("lens_label") or "this network"
    return DerivationReport(
        kind="Methodology",
        subject=f"{scope.get('project_name') or ''} — {lens}".strip(" —"),
        conclusion="How the figures on the KPI screen are calculated",
        summary=(
            "Every number on the KPI screen is produced by a mixed-integer "
            "linear program that minimises the total cost of running your "
            "network subject to its capacity, demand and service constraints. "
            "This document states the rule behind each figure, defines the "
            "quantities in it, and works the rule through with this network's "
            "own values so the arithmetic can be followed to the number on "
            "the screen."
        ),
        method=(
            "The optimiser chooses which sites to open and which corridors to "
            "move volume on. Everything reported is then read back off that "
            "one solved plan: the cost components are the plan's own, the "
            "utilisations are its flows against your stated capacities, and "
            "the service figures are the demand it met against the demand you "
            "gave it. Nothing on the screen is an average of scenarios, an "
            "extrapolation, or an estimate."
        ),
        assumptions=list(scope.get("assumptions") or []),
        limitations=[
            "Figures describe the plan the optimiser produced, which is the "
            "lowest-cost feasible plan for the data supplied. A different "
            "upload produces a different plan.",
            "Costs exclude the solver's internal shortage penalty, which "
            "exists to rank infeasible options and is not money anybody pays.",
            "Inventory holding is decided for the network rather than per "
            "site, so per-facility costs do not sum to the network total.",
        ],
        provenance=(
            f"Source: {scope.get('execution_id') or 'this analysis'} · "
            f"snapshot {scope.get('snapshot_id') or '—'}"
        ),
        generated_at=scope.get("generated_at") or "",
        steps=steps,
    )


__all__ = ["build_kpi_method_report"]
