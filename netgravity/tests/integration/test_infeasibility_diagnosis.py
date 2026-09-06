"""
An infeasible answer that says why.

THE GAP. `allow_shortage=False` asks the solver to serve every unit within
every stated service level. When that is impossible it reports INFEASIBLE —
correctly — and returned nothing else at all. Measured on the case-16 fixture
at 2x demand:

    status      : INFEASIBLE
    warnings    : []
    total_cost  : 0.0
    unmet_demand: 0.0

Every line is true and the set of them is useless. The worst is
`unmet_demand: 0.0`, which means "not computed" and reads as "nothing is
short" on a network that cannot reach part of its demand. A screen rendering
that showed an empty network, not "you are short of capacity".

THE FIX. `netgravity/optimization/infeasibility.py` re-solves ONCE with unmet
demand permitted and produces a diagnosis: the shortfall in units, the markets
it falls in, and the sites the optimiser would open — including any CANDIDATE
the client has proposed but not built.

WHERE IT LIVES, AND WHY NOT IN THE SOLVER. `OptimizationClient` already owns
what happens when a solve proves infeasible — it re-solves to shortage when
the caller asked for that, and otherwise raises — so the diagnosis belongs
beside that decision. `milp.py` is the MILP formulation and is guarded
(`test_extraction_agent.py::test_solver_internals_are_not_edited_by_accident`);
that guard is right that a second solve and a sentence generator are not part
of the model, and it caught the first version of this change sitting there.

THE RULE IT KEEPS. A diagnosis EXPLAINS a failure; it never replaces one. The
result stays INFEASIBLE, its KPIs stay refused, and no cost is reported from
the diagnostic model — whose objective is dominated by a 1e6-per-unit shortage
penalty and is not money anyone pays. That is the same separation
`resilience/rei.py::_service_diagnostic` has always made for a disrupted
network.

These tests cover the whole path: the diagnosis, the client, the exception,
the execution context, the API and the screen.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from netgravity.optimization.milp import solve
from netgravity.orchestrator.engines.deterministic import OptimizationClient
from netgravity.orchestrator.exceptions import SolverInfeasibleError
from netgravity.schemas.network import CanonicalNetwork, FacilityStatus
from netgravity.schemas.results import SolverStatus
from netgravity.tests.fixtures.case16_synthetic import build_case16_network

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _js(*parts: str) -> str:
    return ROOT.joinpath("app", "frontend", "js", *parts).read_text(encoding="utf-8")


def _py(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def grow(network: CanonicalNetwork, factor: float) -> CanonicalNetwork:
    return network.model_copy(update={
        "demands": [d.model_copy(update={"quantity": d.quantity * factor})
                    for d in network.demands],
    })


def unreachable(network: CanonicalNetwork) -> CanonicalNetwork:
    """A market with no inbound lane — broken structurally, not by volume."""
    markets = [f.id for f in network.facilities
               if getattr(f.role, "value", str(f.role)) in ("MARKET", "CUSTOMER")]
    return network.model_copy(update={
        "lanes": [l for l in network.lanes if l.destination_id != markets[0]]})


def diagnosis_of(network: CanonicalNetwork) -> dict:
    """The diagnosis a caller of the product path actually receives."""
    with pytest.raises(SolverInfeasibleError) as caught:
        asyncio.run(OptimizationClient().solve_result(network))
    return dict((caught.value.context or {}).get("infeasibility") or {})


@pytest.fixture(scope="module")
def base() -> CanonicalNetwork:
    return build_case16_network()


@pytest.fixture(scope="module")
def diagnosed(base) -> dict:
    return diagnosis_of(grow(base, 2.0))


# ---------------------------------------------------------------------------
# The diagnosis explains; it does not substitute
# ---------------------------------------------------------------------------

class TestADiagnosisIsNotAResult:

    def test_the_solve_is_still_infeasible(self, base):
        """A diagnosis must never turn "no" into "yes"."""
        assert solve(grow(base, 2.0)).solver.status == SolverStatus.INFEASIBLE
        with pytest.raises(SolverInfeasibleError):
            asyncio.run(OptimizationClient().solve_result(grow(base, 2.0)))

    def test_no_cost_is_reported_from_the_diagnostic_model(self, diagnosed):
        """
        The diagnostic objective is `business_cost + 1e6 x unserved`. Reporting
        any part of it as money is the exact failure this codebase has had to
        undo elsewhere, which is why the diagnosis carries service figures and
        nothing else.
        """
        for money in ("cost", "total_cost", "objective", "business_network_cost",
                      "shortage_penalty_cost"):
            assert money not in diagnosed, money

    def test_the_engines_own_kpis_stay_refused(self, base):
        result = solve(grow(base, 2.0))
        assert result.kpis.total_cost == 0
        assert result.kpis.unmet_demand == 0
        assert result.facility_decisions == []

    def test_a_feasible_network_pays_for_no_second_solve(self, base):
        """It costs a solve. It must run only where the answer is nothing."""
        state = asyncio.run(OptimizationClient().solve_result(base))
        assert getattr(state, "infeasibility", None) is None

    def test_it_is_not_run_where_shortage_was_already_permitted(self):
        """Asking the same question again learns nothing, and would recurse."""
        src = _py("netgravity", "orchestrator", "engines", "deterministic.py")
        block = src[src.index("    async def _diagnose("):]
        block = block[:block.index("    async def _relaxed_shortage_result(")]
        assert "if cfg.allow_shortage:" in block
        assert "return None" in block

    def test_it_can_be_switched_off(self, base):
        network = grow(base, 2.0)
        network = network.model_copy(update={
            "config": network.config.model_copy(
                update={"diagnose_infeasible": False})})
        with pytest.raises(SolverInfeasibleError) as caught:
            asyncio.run(OptimizationClient().solve_result(network))
        assert (caught.value.context or {}).get("infeasibility") is None

    def test_the_solver_formulation_is_untouched(self):
        """
        §20 and the guard in `test_extraction_agent.py`. The diagnosis is
        reporting, not formulation — the first version of this change put a
        second solve inside `milp.py`, and the guard caught it.
        """
        milp = _py("netgravity", "optimization", "milp.py")
        assert "_diagnose_infeasible" not in milp
        assert "InfeasibilityDiagnosis" not in milp


# ---------------------------------------------------------------------------
# What it says
# ---------------------------------------------------------------------------

class TestItSaysWhatTheNetworkCouldNotDo:

    def test_the_shortfall_in_units_and_as_a_share(self, diagnosed):
        assert diagnosed["diagnosed"] is True
        assert diagnosed["unserved_demand"] > 0
        assert diagnosed["total_demand"] > diagnosed["unserved_demand"]
        assert 0 < diagnosed["unserved_rate"] < 1

    def test_the_markets_it_falls_in(self, diagnosed):
        """
        "You are short" is a fact. "MKT_A is short by 600 units" is something a
        planner can act on.
        """
        assert diagnosed["short_markets"]
        first = diagnosed["short_markets"][0]
        assert first["market_id"]
        assert first["unserved"] > 0
        assert first["demand"] >= first["unserved"]

    def test_the_markets_are_listed_worst_first_and_bounded(self, diagnosed):
        shortfalls = [m["unserved"] for m in diagnosed["short_markets"]]
        assert shortfalls == sorted(shortfalls, reverse=True)
        assert len(diagnosed["short_markets"]) <= 5

    def test_the_proposed_site_the_optimiser_would_build(self, diagnosed, base):
        """The sentence an empty dashboard could never produce."""
        proposed = {f.id for f in base.facilities
                    if f.status == FacilityStatus.CANDIDATE}
        assert diagnosed["would_open_candidates"]
        assert set(diagnosed["would_open_candidates"]) <= proposed
        assert set(diagnosed["would_open_candidates"]) <= set(diagnosed["would_open"])
        assert "proposed but not built" in diagnosed["summary"]

    def test_the_summary_says_where_the_figures_came_from(self, diagnosed):
        """
        A reader must be able to tell a diagnostic figure from a plan. The
        summary says so in its own words rather than relying on the caller.
        """
        assert "diagnostic solve" in diagnosed["summary"]
        assert "not a plan" in diagnosed["summary"]
        assert "no cost is reported" in diagnosed["summary"]


class TestAFailureCapacityCannotFixSaysSo:

    def test_a_structural_break_is_named_and_not_called_a_shortfall(self, base):
        """
        A market with no inbound lane fast-fails validation before any model is
        built, so there is nothing to re-solve — and nothing worth re-solving:
        the same check would fail again. Telling a planner they are short of
        capacity here would send them to buy something that cannot help.
        """
        diagnosis = diagnosis_of(unreachable(base))
        assert diagnosis["diagnosed"] is False
        assert "structural check" in diagnosis["summary"]
        assert "No amount of capacity fixes this" in diagnosis["summary"]
        assert diagnosis["unserved_demand"] is None

    def test_a_shortage_run_that_is_still_infeasible_is_a_different_finding(self):
        """
        When even "serve what you can" has no solution, the obstacle is not the
        amount of capacity.
        """
        src = _py("netgravity", "optimization", "infeasibility.py")
        assert "the obstacle is not" in src
        assert 'reason="the diagnostic model was itself infeasible."' in src

    def test_a_failed_diagnosis_never_fails_the_solve(self):
        """
        The diagnosis is an extra. If it raises, the caller must be left with
        exactly what it had before this existed.
        """
        src = _py("netgravity", "optimization", "infeasibility.py")
        block = src[src.index("def diagnose("):]
        assert "except Exception" in block
        assert "diagnosed=False" in block


# ---------------------------------------------------------------------------
# It survives every hop to the screen
# ---------------------------------------------------------------------------

class TestTheReasonReachesTheReader:

    def test_the_exception_really_carries_it(self, base):
        """
        Functional, not textual: the real client, on a network that genuinely
        cannot be solved, and the exception a caller catches.
        """
        with pytest.raises(SolverInfeasibleError) as caught:
            asyncio.run(OptimizationClient().solve_result(unreachable(base)))
        exc = caught.value
        # In the message, because `orchestrator.py` transitions to INFEASIBLE
        # with `exc.message[:200]` and several screens render only that.
        assert "structural check" in exc.message
        # And structured, for a screen that can do better than a paragraph.
        assert (exc.context or {}).get("infeasibility")

    def test_a_relaxed_plan_carries_the_same_two_answers_without_a_second_solve(
            self, base):
        """
        The relaxation IS the diagnostic solve — same network, same costs,
        unmet demand permitted. Running `diagnose()` on top of it would spend a
        full MILP solve to learn what the relaxed plan already knows, on every
        uploaded network (they all set `relax_to_shortage_when_infeasible`).

        So the note the relaxed plan already carried gains the two things it
        lacked, read off the plan itself.
        """
        network = grow(base, 2.0)
        network = network.model_copy(update={
            "config": network.config.model_copy(
                update={"relax_to_shortage_when_infeasible": True})})
        state = asyncio.run(OptimizationClient().solve_result(network))

        note = dict(getattr(state, "solve_relaxation", None) or {})
        assert note.get("strict_solve_status") == "INFEASIBLE"
        assert note.get("unserved_demand") > 0
        # WHERE, and which proposed site it opened to get this far.
        assert note.get("short_markets")
        assert note["short_markets"][0]["market_id"]
        assert note.get("would_open_candidates")

    def test_the_relaxation_path_does_not_run_a_second_diagnostic(self):
        src = _py("netgravity", "orchestrator", "engines", "deterministic.py")
        block = src[src.index("if result.solver.status == SolverStatus.INFEASIBLE:"):]
        block = block[:block.index("raise SolverInfeasibleError")]
        # The relaxation is tried FIRST and returns; `_diagnose` is reached only
        # when nothing else is going to answer.
        assert block.index("_solve_relaxed_to_shortage") < block.index("self._diagnose(")

    def test_the_execution_context_stops_dropping_it(self):
        """
        `executor.py` puts the exception's context on the result as
        `error_context`, and this hop recorded four fields off the same object
        and threw the context away.
        """
        src = _py("netgravity", "orchestrator", "core", "execution_context.py")
        block = src[src.index("self.errors.append({"):]
        block = block[:block.index("\n            })")]
        assert '"context": dict(result.metadata.get("error_context") or {})' in block

    def test_the_api_hands_it_to_the_screen(self):
        src = _py("app", "backend", "api", "scenarios.py")
        assert '"infeasibility": diagnosis' in src
        block = src[src.index("diagnosis = next("):]
        block = block[:block.index("logger.info(")]
        assert '.get("infeasibility")' in block

    def test_the_client_stops_dropping_the_error_context(self):
        """
        `ApplicationError.fromHttp` read `err.details`. Every error this backend
        raises carries `err.context`, so the structured half of every failure
        was discarded at the client boundary on every screen.
        """
        assert "err.details || err.context || {}" in _js("integration", "errors.js")

    def test_the_form_renders_the_figures_rather_than_a_paragraph(self):
        js = _js("scenarios.js")
        assert "function infeasibilityHtml(diagnosis)" in js
        block = js[js.index("function infeasibilityHtml(diagnosis)"):]
        block = block[:block.index("\n}\n")]
        assert "'Cannot be served'" in block
        assert "'Short in'" in block
        assert "'Would build'" in block
        # Nothing invented when the diagnosis did not run.
        assert "if (!diagnosis || !diagnosis.diagnosed) return frag;" in block

    def test_an_uploaded_market_name_cannot_inject_markup(self):
        js = _js("scenarios.js")
        block = js[js.index("function infeasibilityHtml(diagnosis)"):]
        block = block[:block.index("\n}\n")]
        assert "const esc = (t) =>" in block
