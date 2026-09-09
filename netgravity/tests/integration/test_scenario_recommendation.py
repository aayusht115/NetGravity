"""
What the Scenario Planner recommends, and who decided it.

Three things were wrong with the recommendation on this screen, and they are
different kinds of wrong:

  * IT WAS WRITTEN IN THE BROWSER. `rankScenarios()` and
    `renderMultiScenarioTakeCard()` in scenarios.js ranked the rows, picked a
    winner and wrote the sentence. A decision made in JavaScript is invisible
    to the audit trail, untestable from this suite, and free to disagree with
    the same numbers elsewhere on screen.

  * THE SCENARIO'S OWN EXPLANATION WAS THROWN AWAY. The scenario workflow runs
    `reasoning.synthesise` on every simulate — `_reason_and_govern` in
    core/planner.py — and `/simulate` returned none of it. The screen had only
    the network's general briefing to show beside a what-if's numbers.

  * THE HEADLINE COULD REPORT GROWTH AS A SAVING. Measured on a real upload: a
    +30% demand scenario came back 11.2% BELOW the network as it runs. The
    arithmetic was right. The baseline pins twenty sites open and a scenario
    may shut four of them, so the gap it reports is the redesign's saving minus
    the growth's cost — and read off the headline alone, "demand up 30%" was a
    cost reduction.

These tests hold the answers to all three on the BACKEND, where they can be
asserted.
"""

from __future__ import annotations

import uuid

import pytest

from app.backend.api.scenarios import (
    _attribution,
    _capacity_response,
    _capacity_risk,
    _capacity_verdict,
    _comparison_verdict,
    _is_structural,
    _rank_scenarios,
    _service_warning,
)
from app.backend.app import app

DEMO_PROJECT = "pr-demo-case16"
GOOD_PASSWORD = "scenario-rec-test-pw-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def auth(client):
    email = f"rec-{uuid.uuid4().hex}@example.com"
    res = client.post("/api/auth/signup",
                      json={"email": email, "password": GOOD_PASSWORD})
    assert res.status_code == 201, res.get_json()
    return {"Authorization": f"Bearer {res.get_json()['token']}"}


def _kpi(value, status="VALID"):
    return {"value": value, "status": status, "unit": ""}


def _record(sid, *, cost, fill=0.98, reference=None, baseline=100.0,
            name=None, peak=None, action="CHANGE_DEMAND"):
    """One stored scenario, in the shape `/simulate` writes."""
    scenario_kpis = {"business_network_cost": _kpi(cost),
                     "demand_fill_rate": _kpi(fill)}
    if peak is not None:
        scenario_kpis["max_utilization_pct"] = _kpi(peak)
    return {
        "id": sid,
        "name": name or sid,
        "request": {"action": action},
        "baseline_kpis": {"business_network_cost": _kpi(baseline),
                          "demand_fill_rate": _kpi(1.0)},
        "scenario_kpis": scenario_kpis,
        "reference_kpis": ({"business_network_cost": _kpi(reference)}
                           if reference is not None else {}),
    }


# ---------------------------------------------------------------------------
# The ranking
# ---------------------------------------------------------------------------

class TestTheRankingIsDecidedHere:

    def test_cheapest_first_on_the_solvers_own_cost(self):
        rows = _rank_scenarios(
            {"business_network_cost": _kpi(100.0)},
            [_record("B", cost=95.0), _record("A", cost=80.0)])
        assert [r["scenario_id"] for r in rows] == ["A", "B"]
        assert rows[0]["cost_delta"] == -20.0

    def test_a_refused_cost_is_listed_but_never_ranked(self):
        """
        Dropping it would silently shorten the comparison; giving it a position
        would award one it did not earn. It is last, and marked.
        """
        rows = _rank_scenarios(
            {"business_network_cost": _kpi(100.0)},
            [_record("NOPE", cost=None), _record("A", cost=80.0)])
        assert [r["scenario_id"] for r in rows] == ["A", "NOPE"]
        assert rows[1]["comparable"] is False
        assert rows[1]["cost"] is None

    def test_a_non_valid_status_is_never_read_as_a_number(self):
        record = _record("X", cost=1.0)
        record["scenario_kpis"]["business_network_cost"] = {
            "value": 1.0, "status": "NOT_COMPUTABLE"}
        rows = _rank_scenarios({"business_network_cost": _kpi(100.0)}, [record])
        assert rows[0]["cost"] is None

    def test_ties_break_on_the_id_so_the_answer_is_the_same_both_ways(self):
        baseline = {"business_network_cost": _kpi(100.0)}
        forward = _rank_scenarios(baseline, [_record("A", cost=80.0),
                                             _record("B", cost=80.0)])
        backward = _rank_scenarios(baseline, [_record("B", cost=80.0),
                                              _record("A", cost=80.0)])
        assert [r["scenario_id"] for r in forward] == \
               [r["scenario_id"] for r in backward]


# ---------------------------------------------------------------------------
# Where a headline saving actually comes from
# ---------------------------------------------------------------------------

class TestGrowthIsNotReportedAsASaving:
    """
    The measured defect, in miniature: the network runs at 701, the same
    network re-solved with a scenario's own freedom costs 622, and a +30%
    demand scenario costs 640. Against the baseline that is 61 CHEAPER. The
    change itself made it 18 dearer.
    """

    def _rows(self):
        return _rank_scenarios(
            {"business_network_cost": _kpi(701.0)},
            [_record("D30", cost=640.0, reference=622.0, baseline=701.0,
                     name="Demand +30%")])

    def test_the_row_carries_both_halves_separately(self):
        row = self._rows()[0]
        assert row["cost_delta"] == -61.0            # against today
        assert row["reoptimisation_effect"] == -79.0  # the redesign
        assert row["change_effect"] == 18.0           # the growth itself

    def test_the_verdict_says_where_the_difference_comes_from(self):
        verdict = _comparison_verdict(self._rows())
        assert "re-optimising the footprint you already have" in verdict["caveats"][0]
        assert "available without this scenario" in verdict["caveats"][0]
        # And it says which way the change itself pushed.
        assert verdict["attribution"]["change_direction"] == "adds"

    def test_the_amounts_travel_unformatted_so_the_screen_can_render_them(self):
        """
        The currency belongs to the upload and is applied in exactly one place.
        Composing the sentence here printed a bare "167,846,924.60" beside a
        card whose every other figure read "C$167.85M".
        """
        attribution = _attribution(self._rows()[0])
        assert attribution["reoptimisation_amount"] == -79.0
        assert attribution["change_amount"] == 18.0
        # Not one digit in the sentence the backend writes.
        assert not any(ch.isdigit() for ch in attribution["text"]), attribution["text"]

    def test_a_change_that_moves_nothing_says_so(self):
        rows = _rank_scenarios(
            {"business_network_cost": _kpi(701.0)},
            [_record("NOOP", cost=622.0, reference=622.0, baseline=701.0)])
        attribution = _attribution(rows[0])
        assert attribution["change_direction"] == "none"
        assert "None of this difference is the change itself" in attribution["text"]

    def test_no_reference_makes_no_attribution_claim(self):
        """A missing reference is silence, not a guess at the split."""
        rows = _rank_scenarios({"business_network_cost": _kpi(701.0)},
                               [_record("X", cost=640.0, baseline=701.0)])
        assert _attribution(rows[0]) == {}

    def test_a_redesign_worth_nothing_is_not_narrated(self):
        rows = _rank_scenarios({"business_network_cost": _kpi(701.0)},
                               [_record("X", cost=640.0, reference=701.0,
                                        baseline=701.0)])
        assert _attribution(rows[0]) == {}


# ---------------------------------------------------------------------------
# Cheaper is not better
# ---------------------------------------------------------------------------

class TestACostRankingMustNotBuryService:

    def test_a_plan_below_the_service_floor_is_called_out(self):
        warning = _service_warning({"fill_rate": 0.685}, {})
        assert "68.5% of demand" in warning
        assert "not necessarily an acceptable one" in warning

    def test_capacity_risk_is_said_beside_the_cost(self):
        warning = _service_warning({"fill_rate": 0.99}, {"capacity_risk": "High"})
        assert "capacity risk remains high" in warning

    def test_a_healthy_plan_produces_no_warning(self):
        assert _service_warning({"fill_rate": 0.99}, {"capacity_risk": "Low"}) == ""

    def test_serving_less_than_today_is_stated_in_the_caveats(self):
        rows = _rank_scenarios({"business_network_cost": _kpi(100.0),
                                "demand_fill_rate": _kpi(1.0)},
                               [_record("A", cost=80.0, fill=0.90)])
        caveats = " ".join(_comparison_verdict(rows)["caveats"])
        assert "smaller promise" in caveats

    def test_the_floor_comes_from_policy_not_from_this_module(self):
        from netgravity.config.defaults import SERVICE_THRESHOLDS

        floor = SERVICE_THRESHOLDS["fill_rate_floor"]
        assert _service_warning({"fill_rate": floor - 0.001}, {}) != ""
        assert _service_warning({"fill_rate": floor + 0.001}, {}) == ""


class TestTheCapacityRiskBandIsDerivedHere:
    """
    It existed only in `capacityRiskFrom()` in scenario-mapper.js, so the
    ranking's own service warning had nothing to read.
    """

    def test_the_bands_match_the_mapper(self):
        assert _capacity_risk({"max_utilization_pct": _kpi(96.0)}) == "High"
        assert _capacity_risk({"max_utilization_pct": _kpi(80.0)}) == "Medium"
        assert _capacity_risk({"max_utilization_pct": _kpi(40.0)}) == "Low"

    def test_an_unmeasured_network_is_unknown_never_low(self):
        assert _capacity_risk({}) == "Unknown"
        assert _capacity_risk({"max_utilization_pct":
                               _kpi(90.0, "NOT_COMPUTABLE")}) == "Unknown"


# ---------------------------------------------------------------------------
# It ranks; it does not approve
# ---------------------------------------------------------------------------

class TestStructuralChangesStayHuman:

    def test_opening_a_site_is_structural_from_the_request(self):
        assert _is_structural({"request": {"action": "OPEN_FACILITY"}})

    def test_a_capacity_change_that_shut_a_site_is_structural_too(self):
        """Read from the SOLVED topology, not only from what was asked for."""
        assert _is_structural({
            "request": {"action": "CHANGE_CAPACITY"},
            "baseline_facilities": {"DC1": {"isOpen": True}},
            "scenario_facilities": {"DC1": {"isOpen": False}},
        })

    def test_a_scenario_that_moves_no_site_is_analysis(self):
        assert not _is_structural({
            "request": {"action": "CHANGE_DEMAND"},
            "baseline_facilities": {"DC1": {"isOpen": True}},
            "scenario_facilities": {"DC1": {"isOpen": True}},
        })


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

class TestTheCompareEndpoint:

    def _simulate(self, client, auth, name, delta):
        res = client.post(f"/api/scenarios/simulate?project_id={DEMO_PROJECT}",
                          json={"project_id": DEMO_PROJECT, "name": name,
                                "action": "CHANGE_CAPACITY",
                                "facility_ids": ["DC_CENTRAL"],
                                "capacity_delta_units": delta},
                          headers=auth)
        assert res.status_code == 201, res.get_json()
        return res.get_json()

    def test_it_ranks_the_scenarios_it_was_given(self, client, auth):
        first = self._simulate(client, auth, "Cut a little", -500.0)
        second = self._simulate(client, auth, "Cut a lot", -2000.0)
        res = client.post(f"/api/scenarios/compare?project_id={DEMO_PROJECT}",
                          json={"scenario_ids": [first["id"], second["id"]]},
                          headers=auth)
        assert res.status_code == 200, res.get_json()
        body = res.get_json()
        assert len(body["ranked"]) == 2
        assert body["recommended_scenario_id"] in (first["id"], second["id"])
        assert body["verdict"]
        assert "governance" in body

    def test_an_unknown_id_is_refused_rather_than_widened(self, client, auth):
        """
        Falling back to every saved scenario answers a different question than
        the one asked, under the heading of the one asked.
        """
        self._simulate(client, auth, "Something", -500.0)
        res = client.post(f"/api/scenarios/compare?project_id={DEMO_PROJECT}",
                          json={"scenario_ids": ["SCN_nosuchthing"]},
                          headers=auth)
        assert res.status_code == 400
        assert "SCN_nosuchthing" in str(res.get_json())

    def test_an_unknown_project_is_refused_not_answered_emptily(self, client, auth):
        """
        Access is checked before anything is compared, so an id nobody owns
        gets 404 rather than a comparison of nothing — which would render as
        "no scenario beats your network" on a project that does not exist.
        """
        res = client.post("/api/scenarios/compare?project_id=pr-no-such-project",
                          json={}, headers=auth)
        assert res.status_code == 404

    def test_it_requires_authentication(self, client):
        res = client.post(f"/api/scenarios/compare?project_id={DEMO_PROJECT}",
                          json={})
        assert res.status_code == 401


class TestTheScenarioCarriesItsOwnExplanation:

    def test_the_card_is_about_this_scenario(self, client, auth):
        res = client.post(f"/api/scenarios/simulate?project_id={DEMO_PROJECT}",
                          json={"project_id": DEMO_PROJECT,
                                "name": "Explained scenario",
                                "action": "CHANGE_DEMAND",
                                "demand_multiplier": 1.3},
                          headers=auth)
        assert res.status_code == 201, res.get_json()
        body = res.get_json()

        explanation = body["explanation"]
        assert explanation["scope"] == "SCENARIO", (
            "the briefing described the NETWORK on a what-if run")
        assert explanation["entity_id"], "it must name the scenario it is about"

        card = explanation["card"]
        assert card["headline"]
        # Figures are supplied by CODE, so a model can never state one in the
        # wrong currency. Money travels as an amount.
        labels = [f["label"] for f in card["figures"]]
        assert labels == ["Cost", "Demand served", "Sites open"]
        money = card["figures"][0]
        assert money["format"] == "currency"
        assert isinstance(money["amount"], (int, float))

        # And the attribution travels beside the card, as amounts.
        attribution = body["explanation"]["attribution"]
        if attribution:
            assert isinstance(attribution["reoptimisation_amount"], (int, float))
            assert not any(ch.isdigit() for ch in attribution["text"])

    def test_the_capacity_risk_band_reaches_the_record(self, client, auth):
        res = client.post(f"/api/scenarios/simulate?project_id={DEMO_PROJECT}",
                          json={"project_id": DEMO_PROJECT, "name": "Banded",
                                "action": "CHANGE_DEMAND",
                                "demand_multiplier": 1.1},
                          headers=auth)
        body = res.get_json()
        assert body["capacity_risk"] in ("Low", "Medium", "High", "Unknown")
        assert body["baseline_capacity_risk"] in ("Low", "Medium", "High", "Unknown")


# ---------------------------------------------------------------------------
# What the card actually says
#
# Every one of these is a defect the FIRST LIVE RUN put on screen, with a real
# gateway and a real upload. None of them was visible while the model was off,
# because the deterministic template does not go through the same path.
# ---------------------------------------------------------------------------

class TestWhatTheModelWritesReachesTheCardIntact:

    def _live(self, summary, *, scope=None, entity_id=None):
        """One gateway-written briefing, without reaching a gateway."""
        import json

        from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMResponse
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        class _Canned(LLMGateway):
            @property
            def available(self):            # type: ignore[override]
                return True

            def unavailable_reason(self):   # type: ignore[override]
                return ""

            def generate(self, prompt, *, purpose="generic"):  # type: ignore[override]
                return LLMResponse(output=json.dumps({
                    "summary": summary,
                    "key_drivers": ["Demand concentrated in the east"],
                    "risks": ["Service falls where sites close"],
                    "recommendation": "Review the corridors that changed.",
                    "confidence": "MEDIUM", "evidence": [],
                }))

        return ReasoningAgent(_Canned(), runtime=None).reason(
            {"network_state": {"business_network_cost": 1.0}},
            allow_llm=True, single_request=True,
            scope=scope or ReasoningScope.SCENARIO,
            entity_id=entity_id or "SCN_live")

    def test_a_gateway_written_briefing_knows_what_it_is_about(self):
        """
        It came back NETWORK-scoped with no entity on a scenario run, so a
        screen could not tell a what-if's explanation from the network's —
        which is the exact confusion passing `scope` through was meant to end.
        The template path always carried it; only the gateway path did not.
        """
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        result = self._live("Cost falls. Four sites close.")
        assert result.source == "llm"
        assert result.briefing is not None, (
            "the gateway path returned no briefing; /api/insights reads "
            "briefing.kpi_insights directly and would raise")
        assert result.briefing.scope == ReasoningScope.SCENARIO
        assert result.briefing.entity_id == "SCN_live"

    def test_the_conclusion_leads_rather_than_a_label(self):
        """
        The card headed itself "What these results show" and put the model's
        conclusion in the body. The prompt asks for the conclusion in one
        sentence and its meaning in one more; the two are separated.
        """
        from netgravity.orchestrator.explanation_service import build_card

        result = self._live("Cost falls by closing four sites. "
                            "The saving is mostly a smaller footprint.")
        card = build_card(result)
        assert card["headline"] == "Cost falls by closing four sites."
        assert card["meaning"].startswith("The saving is mostly")
        assert "What these results show" not in card["headline"]

    def test_no_sentence_is_cut_in_half(self):
        """
        "...but produces unse" — a hard slice at 260 characters, on the card
        that leads the screen.
        """
        from netgravity.orchestrator.explanation_service import build_card

        long_tail = ("Cost falls. " + "The plan reroutes volume through the "
                     "remaining distribution centres and leaves demand "
                     "unserved in the maritime provinces. " * 4)
        card = build_card(self._live(long_tail))
        body = card["meaning"]
        assert len(body) <= 260
        # Ends on a boundary a reader recognises, never mid-word.
        assert body.endswith((".", "!", "?", "\u2026")), repr(body[-40:])

    def test_one_long_sentence_still_gets_a_body(self):
        """
        Live, on a two-scenario comparison, the model answered in a single long
        sentence. `first_sentence` returned all of it, the headline trimmed it
        at 140 characters, and the duplication guard then blanked the body —
        because a truncation "starts with" the text it truncates. The card
        rendered a fragment ending in an ellipsis with nothing under it, and
        the rest of the sentence existed nowhere on the screen.
        """
        from netgravity.orchestrator.explanation_service import build_card

        sentence = ("This means the company can lower spend while achieving a "
                    "higher fill rate than the other option, but the impact on "
                    "service and capacity has to be weighed before anyone "
                    "commits to it")
        card = build_card(self._live(sentence))
        assert card["headline"].endswith("\u2026")
        assert card["meaning"], "the body was dropped as a duplicate of its own truncation"
        assert card["meaning"].endswith("commits to it")

    def test_a_sentence_too_long_to_be_a_headline_does_not_raise(self):
        """
        `KPIInsight.headline` is capped at 140 characters. Building the
        briefing with a longer one raised a pydantic ValidationError inside
        `_llm`, which `reason()` does not catch — so it would have escaped a
        method whose contract is that it never raises, on the request path of
        every scenario simulate.
        """
        result = self._live("x" * 400)
        assert result.briefing is not None
        assert len(result.briefing.kpi_insights[0].headline) <= 140

    def test_the_grounding_marker_never_reaches_a_reader(self):
        """
        `[UNGROUNDED CLAIM REMOVED ...]` is validation text for an audit
        trail. A sentence that lost its number reads as a claim with no
        evidence, so the whole sentence goes and the loss is counted in the
        technical detail instead.
        """
        from netgravity.orchestrator.explanation_service import build_card
        from netgravity.orchestrator.reasoning.card import count_redactions

        result = self._live("Cost falls to [UNGROUNDED CLAIM REMOVED — no "
                            "authoritative value] this year. Four sites close.")
        card = build_card(result)
        rendered = " ".join([card["headline"], card["meaning"], card["warning"],
                             card["next_step"], *card["details"]])
        assert "UNGROUNDED CLAIM REMOVED" not in rendered, rendered
        assert count_redactions(result.summary) >= 1
        assert any("could not be matched" in d for d in card["details"]), card["details"]


class TestTheWritingRulesDoNotCostTheAnswer:
    """
    The failure mode this whole feature degrades through, silently.

    The gateway caps OUTPUT at 2,000 tokens and the backing model bills its
    internal reasoning to that same allowance. Measured against the live
    gateway on a 20-facility Canadian network, a seven-bullet block of prose
    writing rules produced `output_tokens=1984` and ZERO characters of visible
    text — twice. The scenario still rendered, because the deterministic
    template catches it, so nothing looked broken: the AI recommendation was
    simply never written, and two requests were spent from a shared budget
    finding that out.

    Every rule survives; each is one clause. This holds the size, because the
    natural way to add the eighth rule is another sentence.
    """

    #: Characters of instruction after the evidence. Measured at 780 with the
    #: rules as they stand. The number is a ceiling with room for one more
    #: rule, not a target.
    INSTRUCTION_BUDGET = 900

    def _prompt(self):
        from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMResponse
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        captured = {}

        class _Capture(LLMGateway):
            @property
            def available(self):            # type: ignore[override]
                return True

            def unavailable_reason(self):   # type: ignore[override]
                return ""

            def generate(self, prompt, *, purpose="generic"):  # type: ignore[override]
                captured["prompt"] = prompt
                return LLMResponse(output='{"summary":"A. B.","confidence":"LOW"}')

        ReasoningAgent(_Capture(), runtime=None).reason(
            {"network_state": {"business_network_cost": 1.0}},
            allow_llm=True, single_request=True)
        return captured["prompt"]

    def test_the_instructions_stay_inside_the_budget(self):
        prompt = self._prompt()
        tail = prompt[prompt.index("RULES:"):]
        assert len(tail) <= self.INSTRUCTION_BUDGET, (
            f"the response contract is {len(tail)} characters; past roughly "
            f"{self.INSTRUCTION_BUDGET} the model spends its whole output "
            f"allowance deliberating and returns nothing")

    def test_every_rule_is_still_stated(self):
        """Terser, not fewer. Each of these prevents a specific defect."""
        rules = self._prompt()
        rules = rules[rules.index("RULES:"):]
        for phrase in ("no figures", "Third person", "Plain business English",
                       "Name the real things", "never a placeholder",
                       "No urgency", "say both", "Say each thing once"):
            assert phrase in rules, phrase

    def test_it_does_not_ask_the_model_to_verify_its_own_figures(self):
        """
        The `claims` array and "every number you write is checked" were what
        consumed the budget originally. Grounding runs in code afterwards.
        """
        prompt = self._prompt()
        assert '"claims"' not in prompt
        assert "Do not verify or recompute" in prompt


class TestTheEvidenceFitsInsideTheAnswer:
    """
    The reason the AI recommendation was silently a template.

    The backing model bills its internal reasoning to the same 2,000-token
    OUTPUT budget it writes with, so how much it deliberates scales with how
    much it is handed. Measured on the live gateway with a 20-facility,
    12-period Canadian network: `output_tokens=2000` with the JSON truncated
    mid-structure, then `output_tokens=1984` with no visible text at all. The
    template caught both, so nothing on screen looked wrong.

    The prompt carried up to 40,000 characters of pretty-printed payload, and
    two copies of the largest block in it.
    """

    def _bounded(self, payload):
        import json

        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        return json.loads(ReasoningAgent._bounded_evidence(payload))

    def test_the_solved_state_is_carried_once(self):
        """
        `synthesise` writes it to `scenario` and again to `optimization`
        because the deterministic template reads both names. The model does
        not need it twice.
        """
        state = {"business_network_cost": 1.0, "demand_fill_rate": 0.98}
        out = self._bounded({"scenario": state, "optimization": dict(state)})
        assert out["optimization"] == state
        assert out["scenario"] == "same as 'optimization' above"

    def test_two_genuinely_different_states_are_both_carried(self):
        """The dedupe is on CONTENT. A baseline beside a scenario is two facts."""
        out = self._bounded({"network_state": {"business_network_cost": 1.0},
                             "scenario": {"business_network_cost": 2.0}})
        assert out["network_state"] == {"business_network_cost": 1.0}
        assert out["scenario"] == {"business_network_cost": 2.0}

    def test_a_long_list_is_cut_and_says_so(self):
        """
        A narrative that says "three sites" about a network of twenty is worse
        than one that knows it was shown eight rows of twenty.
        """
        out = self._bounded({"lanes": {"rows": [{"id": i} for i in range(20)]}})
        rows = out["lanes"]["rows"]
        assert len(rows) == 9                      # eight rows plus the note
        assert rows[-1] == "...12 more rows not shown here; 20 in total"

    def test_a_short_list_is_untouched(self):
        out = self._bounded({"lanes": {"rows": [{"id": i} for i in range(4)]}})
        assert out["lanes"]["rows"] == [{"id": i} for i in range(4)]

    def test_the_biggest_blocks_are_cut_harder_than_the_rest(self):
        """
        MEASURED, not guessed. On a scenario run the payload was 11,807
        characters and `rei` alone was 5,704 of them — one row per facility,
        each a full exposure decomposition. The model bills its internal
        deliberation to the same 2,000-token budget it writes with, so half a
        prompt of resilience rows is paid for out of the words the reader
        gets: the reply came back truncated mid-JSON often enough that the
        scenario card was routinely written by the fallback on a build with a
        working gateway.

        The KIND of evidence survives — the block is there, it still says how
        many rows exist, and a briefing can still cite the most exposed site.
        What goes is the tail nothing cites.
        """
        out = self._bounded({"rei": {"facilities": [{"id": i} for i in range(20)]}})
        rows = out["rei"]["facilities"]
        assert len(rows) == 4                      # three rows plus the note
        assert rows[-1] == "...17 more rows not shown here; 20 in total"

    def test_a_narrowed_block_still_says_how_many_it_left_out(self):
        """
        The whole reason a cut list carries a note. A briefing that says "of
        twenty sites" needs to know there were twenty.
        """
        out = self._bounded({"facilities": {"rows": [{"id": i} for i in range(9)]}})
        assert out["facilities"]["rows"][-1].endswith("9 in total")

    def test_it_stays_inside_the_character_bound(self):
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        huge = {"kpis": {str(i): "x" * 400 for i in range(200)}}
        assert len(ReasoningAgent._bounded_evidence(huge)) \
            <= ReasoningAgent._EVIDENCE_CHARS

    def test_the_template_still_reads_the_untrimmed_payload(self):
        """
        The bound is for the PROMPT. Trimming what the deterministic template
        reads would change every figure it reports.
        """
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        payload = {"network_state": {"business_network_cost": 4_200_000.0,
                                     "demand_fill_rate": 0.982,
                                     "total_demand": 100.0, "served_demand": 98.2},
                   "rei": {"facilities": [{"id": i} for i in range(20)]}}
        before = {k: repr(v) for k, v in payload.items()}
        result = ReasoningAgent().reason(payload, allow_llm=False)
        assert result.source == "template"
        assert {k: repr(v) for k, v in payload.items()} == before, (
            "the payload was mutated; the template and the prompt must not "
            "share a trimmed copy")


class TestTheSharedBudgetIsSpentOnlyWhereItBuysSomething:
    """
    The gateway's allowance is cumulative and shared across everyone using it.
    Every request this product makes has to answer a question a reader can see.
    """

    def test_a_comparison_of_one_asks_no_model_anything(self):
        """
        There are no alternatives to weigh, so the briefing would have nothing
        to compare — `comparison_reasoning_payload` returns no alternatives for
        a single row. The scenario's own briefing, produced by its run at no
        further cost, is what answers that case.
        """
        from app.backend.api import scenarios as api

        calls = []
        original = api.explanation_reasoning_agent
        api.explanation_reasoning_agent = lambda: calls.append(1)
        try:
            out = api._comparison_explanation(
                "pr-x",
                [{"scenario_id": "A", "name": "A", "cost": 1.0, "comparable": True}],
                {"recommended_scenario_id": "A", "verdict": "A."}, {})
        finally:
            api.explanation_reasoning_agent = original

        assert out == {}
        assert calls == [], "a set of one reached the model"

    def test_two_scenarios_do_produce_a_comparison_briefing(self):
        """The guard is on the SIZE of the set, not a switch that turns it off."""
        from app.backend.api import scenarios as api

        reached = []
        original = api.explanation_reasoning_agent

        def _spy():
            reached.append(1)
            return original()

        api.explanation_reasoning_agent = _spy
        try:
            api._comparison_explanation(
                "pr-x",
                [{"scenario_id": "A", "name": "A", "cost": 1.0, "comparable": True},
                 {"scenario_id": "B", "name": "B", "cost": 2.0, "comparable": True}],
                {"recommended_scenario_id": "A", "verdict": "A is cheaper."}, {})
        finally:
            api.explanation_reasoning_agent = original

        assert reached, "a real comparison produced no briefing at all"

    def test_the_verdict_counts_options_in_plain_english(self):
        """"1 other option(s) compared" was the leading line of the card."""
        rows = _rank_scenarios(
            {"business_network_cost": _kpi(100.0)},
            [_record("A", cost=80.0), _record("B", cost=90.0)])
        assert "1 other option compared" in _comparison_verdict(rows)["verdict"]

        rows = _rank_scenarios(
            {"business_network_cost": _kpi(100.0)},
            [_record("A", cost=80.0), _record("B", cost=90.0),
             _record("C", cost=95.0)])
        assert "2 other options compared" in _comparison_verdict(rows)["verdict"]

    def test_one_finding_is_not_printed_twice(self):
        """
        The engine's verdict and the model's headline about the same ranking
        were both rendered. An exact-string check caught none of it, because
        they say the same thing in different words.
        """
        js = _asset("scenarios.js")
        assert "function saysTheSameThing(" in js
        # The guard travelled with the paragraph it guards: the model's
        # headline is rendered by `narrativeHtml` now, below the answer rather
        # than second on the card.
        block = js[js.index("function narrativeHtml("):]
        block = block[:block.index("\n}\n")]
        assert "!saysTheSameThing(card.headline, verdict)" in block

    def test_the_finding_comes_before_what_to_do_about_it(self):
        """
        THE ORDER, CORRECTED TWICE, AND THIS IS WHY.

        First pass: the reader met the verdict, then the model's headline,
        then a paragraph restating the cost in a different format, then the
        attribution arithmetic, then a figure strip, then the capacity
        account, then two warnings — and the recommended actions ninth, below
        the fold. Every one of those was true; the order was the defect. So
        the actions were moved up, above the prose.

        That over-corrected. It put the buttons above the reasoning, which
        asks a reader to act before they have been told why — and this card is
        read by people who have to justify the decision to somebody else. The
        finding and its evidence come first now, the actions follow them, and
        the ways into the detail sit below both.

        Verdict, what it means, the figures behind it, what it asks of the
        network, the risk — then what to do.
        """
        js = _asset("scenarios.js")
        block = js[js.index("container.innerHTML = takeHeadHtml("):]
        block = block[:block.index("container.querySelectorAll(")]
        order = [block.index(part) for part in (
            "scn-take-headline",          # the conclusion
            "narrativeHtml(",             # what it means
            "atAGlanceHtml(",             # the facts behind it
            "capacityResponseHtml(",      # what it asks of the network
            "warningBandHtml(",           # what not to miss
            "takeActionsHtml(",           # what to do about it
            "takeFooterHtml(",            # and how to look closer
        )]
        assert order == sorted(order), (
            "the card must read conclusion -> reasoning -> evidence -> risk "
            "-> action -> the way in")

    def test_reading_the_detail_is_not_a_recommended_action(self):
        """
        "Review the proposed changes" was the FIRST recommended action on
        every scenario ever solved, whatever the solve found. It is not a
        recommendation: it tells a reader to look at the screen they are
        already looking at, and it pushed the interventions that answer the
        question — add capacity, reopen a site, build one — below it.

        A senior reader opens this card to learn what to do about their
        network. Reading the detail and asking the assistant are how they get
        from the summary to the evidence; they belong under the answer, not
        among it.
        """
        js = _asset("scenarios.js")
        actions = js[js.index("function recommendedActions("):]
        actions = actions[:actions.index("\n}\n")]
        assert "Review the proposed changes" not in actions
        assert "openScenarioDrawer(" not in actions, (
            "opening the drawer is a way in, not a thing to do about the network")
        # It is still reachable — moved, not dropped.
        assert "function takeFooterHtml(" in js
        footer = js[js.index("function takeFooterHtml("):]
        footer = footer[:footer.index("\n}\n")]
        assert "scn-open-detail" in footer
        assert "scn-download-doc" in footer

    def test_the_recommendations_are_the_servers_not_the_screens(self):
        """
        This list used to be derived in a render function, from the same
        capacity block the backend already had. So the reasoning behind a
        recommendation lived in the browser where it could not be audited, and
        had to be written a second time for the document — two definitions of
        what this application recommends, free to disagree.
        """
        js = _asset("scenarios.js")
        actions = js[js.index("function recommendedActions("):]
        actions = actions[:actions.index("\n}\n")]
        assert "recommended_actions" in actions, "the comparison's own list"
        assert "scn.recommendedActions" in actions, "the record's, as a fallback"
        for key in ("ADD_CAPACITY", "OPEN_NEW_FACILITY", "REOPEN_FACILITY"):
            assert key in actions, key
        # Nothing is re-derived here: no threshold, no capacity arithmetic.
        assert "at_ceiling" not in actions, actions
        assert "capacityResponse" not in actions, actions

    def test_a_finding_that_needs_no_action_is_still_an_answer(self):
        """
        A scenario that fills nothing and strands nothing has no intervention
        to recommend, and an empty "Recommended actions" heading answers
        nothing. The server states the finding instead, and the screen draws
        it as prose — a button would contradict the sentence.
        """
        js = _asset("scenarios.js")
        render = js[js.index("function takeActionsHtml("):]
        render = render[:render.index("\n}\n")]
        assert "a.statement" in render
        assert "is-statement" in render

    def test_the_governance_verdict_is_a_statement_not_a_control(self):
        """
        "This one is a human decision" was a DISABLED BUTTON at the head of
        "Recommended actions" — a control that looks pressable and does
        nothing, above the controls that do. It is a fact about the change.
        """
        js = _asset("scenarios.js")
        assert "function governanceHtml(" in js
        actions = js[js.index("function recommendedActions("):]
        actions = actions[:actions.index("\n}\n")]
        assert "This one is a human decision" not in actions
        # And nothing in the list is inert any more. Scoped to the renderer
        # itself: the window used to run to the next named function, and a
        # download handler added between them put `button.disabled` — a
        # perfectly good busy state on an unrelated control — inside it.
        render = js[js.index("function takeActionsHtml("):]
        render = render[:render.index("\n}\n")]
        assert "disabled" not in render, render

    def test_the_fallback_reason_is_shown_rather_than_hidden(self):
        """
        The template fallback is meant to be seamless, and that is exactly what
        made it invisible: the reasoning degraded and nothing said so. The
        reason sits behind "How this was decided".
        """
        js = _asset("scenarios.js")
        block = js[js.index("function renderMultiScenarioTakeCard()"):]
        # And they belong to the briefing whose words are above them, not to
        # the other one.
        assert "const briefing = aboutTheSet ? comparison.explanation : focus.explanation;" in block
        assert "briefing.grounding && briefing.grounding.warnings" in block


def _asset(name):
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app" / "frontend" / "js"
    return (root / name).read_text(encoding="utf-8")


class TestAScenarioNameIsNotANumericClaim:
    """
    Grounding struck a scenario's own title out of the sentence that named it.

    People name scenarios the way they describe them — "Freight +15%",
    "Demand +30%". Measured on a live two-scenario comparison, "+15%" was read
    as a percentage claim, compared against the nearest percentage fact in the
    payload (a 7.4-point fill-rate gap), found to disagree, and replaced with
    "[UNGROUNDED CLAIM REMOVED ...]". The card's headline then came out empty,
    because a sentence that lost a figure is dropped rather than shown with a
    hole in it, and the recommendation lost its first sentence with it.

    Referring to a thing by its name asserts nothing about the results.
    """

    def _compare(self):
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        from netgravity.orchestrator.reasoning.comparison_evidence import (
            comparison_reasoning_payload,
        )
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        rows = [
            {"scenario_id": "A", "name": "Freight +15%", "cost": 116_500_000.0,
             "cost_delta": -100_071_682.0, "fill_rate": 0.781, "comparable": True},
            {"scenario_id": "B", "name": "Demand +30%", "cost": 133_492_000.0,
             "cost_delta": -83_079_339.0, "fill_rate": 0.855, "comparable": True},
        ]
        payload = comparison_reasoning_payload(
            ranked=rows, recommended_scenario_id="A",
            verdict="Freight +15% costs less.", baseline_cost=216_000_000.0)
        return ReasoningAgent().reason(payload, scope=ReasoningScope.COMPARISON,
                                       allow_llm=False)

    def test_the_name_survives_grounding(self):
        from netgravity.orchestrator.explanation_service import build_card

        card = build_card(self._compare())
        assert card["headline"] == "Freight +15% against Demand +30%"
        assert "UNGROUNDED" not in card["headline"]
        assert "Demand +30%" in card["meaning"]

    def test_nothing_is_reported_as_contradicted(self):
        result = self._compare()
        assert result.validation_warnings == [], result.validation_warnings

    def test_a_real_figure_in_the_same_sentence_is_still_policed(self):
        """
        The names are masked, not the numbers beside them. A claim the results
        do not support must still be caught.
        """
        from netgravity.orchestrator.validation.numeric_grounding import (
            ground_narrative,
        )

        payload = {"comparison": {"recommended_name": "Freight +15%",
                                  "recommended_cost": 100.0}}
        report = ground_narrative(
            "Freight +15% costs 999,999.00 in this plan.", payload)
        assert report.contradicted or report.unsupported, (
            "masking the name also stopped the figure beside it being checked")

    def test_only_name_shaped_keys_are_protected(self):
        from netgravity.orchestrator.validation.numeric_grounding import _proper_names

        found = _proper_names({
            "comparison": {"recommended_name": "Freight +15%",
                           "verdict": "Something 42% here"},
            "rows": [{"name": "Demand +30%"}, {"name": "No digits here"}],
        })
        assert "Freight +15%" in found
        assert "Demand +30%" in found
        # A verdict is prose, not a name, and a name with no digit cannot be
        # mistaken for a figure in the first place.
        assert "Something 42% here" not in found
        assert "No digits here" not in found


class TestTheNextStepReadsAsOneStep:

    def test_the_gerund_becomes_an_instruction(self):
        """
        Stripping "I recommend " left "Reviewing X with the people who would
        have to carry it out" — a gerund where the card wants an instruction.
        """
        from netgravity.orchestrator.reasoning.card import plain_voice

        out = plain_voice("I recommend reviewing Nagpur with the people who "
                          "would have to carry it out.")
        assert out.startswith("Consider reviewing Nagpur"), out

    def test_the_engine_does_not_say_it_is_the_one_deciding(self):
        from netgravity.orchestrator.reasoning.card import plain_voice

        out = plain_voice("It is a decision, and not one I make.")
        assert "I make" not in out
        assert "not one this analysis makes" in out


# ---------------------------------------------------------------------------
# What the change asks of the network
#
# Measured, on a real upload: a +50% demand scenario was answered with "review
# the proposed changes" and "ask the assistant about this scenario" — the same
# two entries a +5% scenario got, and neither of them the question a planner
# raising demand by half is actually asking. Which sites have to carry it, how
# hard do they have to run, and where has the network nothing left.
#
# None of that existed anywhere. The per-facility values were on the record
# already, and nothing read them.
# ---------------------------------------------------------------------------

class _Facility:
    def __init__(self, fid, name, region=None, role="DC"):
        self.id = fid
        self.name = name
        self.region = region
        self.role = role


class _Engine:
    """The two stores `_facility_meta` reads names and regions from."""

    def __init__(self, facilities):
        network = type("N", (), {"facilities": facilities})()
        holder = type("H", (), {"network": network})()
        self.snapshots = type("S", (), {"get": lambda _self, _id: holder})()
        self.scenarios = type("C", (), {
            "get": lambda _self, _id: (_ for _ in ()).throw(KeyError)})()


def _state(*, util, throughput, capacity, is_open=True):
    return {"utilPct": util, "throughput": throughput,
            "capacity": capacity, "isOpen": is_open}


_SITES = _Engine([
    _Facility("F1", "Toronto DC", "Ontario"),
    _Facility("F2", "Calgary DC", "Alberta"),
    _Facility("F3", "Halifax DC", "Nova Scotia"),
])


class TestWhatTheChangeAsksOfTheNetwork:

    def test_a_site_the_plan_fills_is_named_with_what_it_must_carry(self):
        out = _capacity_response(
            _SITES, "snap", None,
            {"F1": _state(util=60.0, throughput=600.0, capacity=1000.0)},
            {"F1": _state(util=100.0, throughput=1000.0, capacity=1000.0)},
            {"unserved_demand": _kpi(0.0)})

        assert out["at_ceiling_count"] == 1
        row = out["at_ceiling"][0]
        # The name and the region, so the recommendation can say where.
        assert row["name"] == "Toronto DC"
        assert row["region"] == "Ontario"
        # The utilisation it has to reach, and how much of it is new.
        assert row["util_pct"] == 100.0
        assert row["baseline_util_pct"] == 60.0
        assert row["added_units"] == 400.0

    def test_a_rounding_tail_is_not_headroom(self):
        """
        A solve fills a site to the unit and reports 99.97%. Telling a planner
        that site has room is telling them something false.
        """
        out = _capacity_response(
            _SITES, "snap", None, {},
            {"F1": _state(util=99.97, throughput=999.7, capacity=1000.0)},
            {"unserved_demand": _kpi(0.0)})
        assert out["at_ceiling_count"] == 1

    def test_a_site_working_harder_with_room_left_is_reported_separately(self):
        out = _capacity_response(
            _SITES, "snap", None,
            {"F2": _state(util=40.0, throughput=400.0, capacity=1000.0)},
            {"F2": _state(util=90.0, throughput=900.0, capacity=1000.0)},
            {"unserved_demand": _kpi(0.0)})
        assert out["at_ceiling_count"] == 0
        assert [r["name"] for r in out["working_harder"]] == ["Calgary DC"]
        assert out["working_harder"][0]["headroom_units"] == 100.0

    def test_a_site_the_plan_closed_is_capacity_not_utilisation(self):
        """
        Hydration writes `utilPct = 0` for a site the solve did not open, so a
        closed site reads as an empty one. It is the OPEN FLAG that separates
        them, and a closed site's capacity is the thing worth reporting: it is
        available without building anything.
        """
        out = _capacity_response(
            _SITES, "snap", None,
            {"F3": _state(util=30.0, throughput=300.0, capacity=1000.0)},
            {"F3": _state(util=0.0, throughput=0.0, capacity=1000.0,
                          is_open=False)},
            {"unserved_demand": _kpi(500.0)})
        assert out["idle_count"] == 1
        assert out["idle"][0]["name"] == "Halifax DC"
        assert out["idle_capacity_units"] == 1000.0
        # A closed site is not counted as headroom on the running network.
        assert out["open_headroom_units"] is None

    def test_a_new_site_is_proposed_only_where_nothing_else_is_left(self):
        """
        The whole point of the region test. Alberta has a site at its ceiling
        and nothing else; Ontario has a site at its ceiling AND one with room.
        Recommending a build in Ontario would be recommending a spend against a
        constraint that is not binding there.
        """
        out = _capacity_response(
            _Engine([_Facility("F1", "Toronto DC", "Ontario"),
                     _Facility("F2", "Ottawa DC", "Ontario"),
                     _Facility("F3", "Calgary DC", "Alberta")]),
            "snap", None, {},
            {"F1": _state(util=100.0, throughput=1000.0, capacity=1000.0),
             "F2": _state(util=20.0, throughput=200.0, capacity=1000.0),
             "F3": _state(util=100.0, throughput=500.0, capacity=500.0)},
            {"unserved_demand": _kpi(300.0)})

        assert [r["region"] for r in out["regions_without_room"]] == ["Alberta"]

    def test_a_region_with_something_closed_is_told_to_reopen_not_build(self):
        out = _capacity_response(
            _Engine([_Facility("F1", "Calgary DC", "Alberta"),
                     _Facility("F2", "Edmonton DC", "Alberta")]),
            "snap", None, {},
            {"F1": _state(util=100.0, throughput=500.0, capacity=500.0),
             "F2": _state(util=0.0, throughput=0.0, capacity=800.0,
                          is_open=False)},
            {"unserved_demand": _kpi(300.0)})
        assert out["regions_without_room"] == []
        assert out["idle_capacity_units"] == 800.0

    def test_an_upload_without_regions_says_so_rather_than_guessing(self):
        out = _capacity_response(
            _Engine([_Facility("F1", "Site One", None)]),
            "snap", None, {},
            {"F1": _state(util=100.0, throughput=500.0, capacity=500.0)},
            {"unserved_demand": _kpi(300.0)})
        assert out["regions_known"] is False
        assert out["regions_without_room"] == []

    def test_a_solve_with_no_facility_state_returns_nothing(self):
        assert _capacity_response(_SITES, "snap", None, {}, {},
                                  {"unserved_demand": _kpi(0.0)}) == {}

    def test_a_metric_the_kpi_layer_refused_does_not_become_a_number(self):
        out = _capacity_response(
            _SITES, "snap", None, {},
            {"F1": _state(util=None, throughput=None, capacity=None)},
            {"unserved_demand": _kpi(9.0, "NOT_COMPUTABLE")})
        assert out["at_ceiling"] == []
        assert out["unserved_units"] is None
        assert out["open_headroom_units"] is None
        assert out["verdict"] == ""


class TestTheVerdictNamesTheBindingConstraint:
    """
    The distinction a cost ranking hides, and the one that decides whether
    spending on capacity would achieve anything: demand can go unserved on a
    network with room to spare, because capacity out of reach of the demand —
    by distance, by lane, or by the service promise — cannot serve it.

    Measured on a real +30% run: 139,054 units unserved against 3,020,699
    units of unused room. "Add capacity" would have been advice to spend money
    on a constraint that was not binding.
    """

    def test_room_to_spare_says_capacity_is_not_what_is_binding(self):
        text = _capacity_verdict(139054.0, 3020699.0, 0.0, 6)
        assert "not what is binding" in text
        assert "3,020,699" in text
        assert "139,054" in text

    def test_full_sites_with_something_closed_says_reopen_first(self):
        text = _capacity_verdict(500.0, 10.0, 8000.0, 3)
        assert "Reopening comes before building" in text
        assert "8,000" in text

    def test_nothing_left_at_all_says_exactly_that(self):
        text = _capacity_verdict(500.0, 0.0, 0.0, 3)
        assert "no room left" in text
        assert "nothing closed to reopen" in text

    def test_a_plan_that_serves_everything_does_not_manufacture_a_problem(self):
        assert "serves all of the demand" in _capacity_verdict(0.0, 500.0, 0.0, 2)
        assert "without filling any site" in _capacity_verdict(0.0, 500.0, 0.0, 0)

    def test_an_unmeasured_shortfall_says_nothing(self):
        assert _capacity_verdict(None, 100.0, 0.0, 4) == ""
