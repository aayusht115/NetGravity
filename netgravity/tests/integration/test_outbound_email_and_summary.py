"""
What the mail path would have got wrong on the day it was switched on, and
what the recommendation card was making a reader assemble for themselves.

THE MAIL PATH
-------------
Both faults here are invisible in the state this repository ships in — no
outbound credential — and both appear the moment one is configured.

  * A REFUSED RECIPIENT READ AS A DELIVERED ONE. `SMTP.send_message` raises
    only when it rejects EVERY address. Reject one of four and it returns a
    dict of the failures and no exception, and the old `_send_live` returned
    None and dropped it. Ask four people for a missing column with one address
    mistyped, and the screen said all four were asked.

  * A REJECTED MESSAGE READ AS AN UNCONFIGURED SERVER. The dispatch endpoint
    rebuilt the three-way verdict from the sender's flags, testing `stubbed`
    before `failed` — and a degraded live failure sets both. So a wrong SMTP
    password produced "No outbound mail server is configured on this
    deployment", sending the operator to set a variable that was already set.

THE RECOMMENDATION
------------------
Reported as "difficult to interpret", and measured on the +50% demand run: the
verdict, then a headline about the BASELINE's cost under the scenario's own
name, then the same cost again unformatted ("556,658,494.26" beside the tile
reading "C$556.66M"), then three sentences of attribution arithmetic, then the
figures, then the capacity account, then two warnings — and what to do about
any of it ninth, below the fold.
"""

from __future__ import annotations

import pathlib

import pytest

from netgravity.action_agent.config import ActionAgentConfig
from netgravity.action_agent.email_sender import EmailSender

FRONTEND = pathlib.Path(__file__).resolve().parents[3] / "app" / "frontend"


def _js(*parts: str) -> str:
    return (FRONTEND / "js").joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def scenarios_js() -> str:
    return _js("scenarios.js")


@pytest.fixture(scope="module")
def insight_js() -> str:
    return _js("insight-detail.js")


@pytest.fixture
def unconfigured() -> ActionAgentConfig:
    """
    No outbound credential — the state this repository ships in.

    Stated explicitly rather than by clearing the environment, so the test
    says what it is testing and cannot pass or fail on what the machine
    running it happens to have set.
    """
    return ActionAgentConfig(smtp_host=None, email_api_key=None)


# ---------------------------------------------------------------------------
# Delivery, reported as what it was
# ---------------------------------------------------------------------------

class TestARefusedRecipientIsNeverReportedAsAsked:

    def test_a_partial_rejection_is_its_own_outcome(self, monkeypatch):
        """
        Some of these people were asked and some were not. Calling that "sent"
        loses the ones who were not; calling it "failed" would have the sender
        ask the first group a second time.
        """
        cfg = ActionAgentConfig(smtp_host="smtp.example.com")
        sender = EmailSender(cfg)
        monkeypatch.setattr(
            EmailSender, "_send_live",
            lambda self, **kw: {"typo@exmaple.com": (550, b"No such user")})

        result = sender.send(to=["real@example.com", "typo@exmaple.com"],
                             subject="s", body="b")

        assert result.outcome == "partial"
        assert result.refused == ["typo@exmaple.com"]
        assert result.delivered == ["real@example.com"]
        # And the reason, so the address can be corrected.
        assert "550" in result.notes
        assert "No such user" in result.notes

    def test_every_address_refused_is_a_failure_not_a_partial(self, monkeypatch):
        cfg = ActionAgentConfig(smtp_host="smtp.example.com")
        sender = EmailSender(cfg)
        monkeypatch.setattr(
            EmailSender, "_send_live",
            lambda self, **kw: {"a@x.com": (550, b"nope"), "b@x.com": (550, b"nope")})

        result = sender.send(to=["a@x.com", "b@x.com"], subject="s", body="b")
        assert result.outcome == "failed"
        assert result.delivered == []

    def test_a_clean_send_is_sent(self, monkeypatch):
        cfg = ActionAgentConfig(smtp_host="smtp.example.com")
        monkeypatch.setattr(EmailSender, "_send_live", lambda self, **kw: {})
        result = EmailSender(cfg).send(to=["a@x.com"], subject="s", body="b")
        assert result.outcome == "sent"
        assert result.delivered == ["a@x.com"]
        assert result.refused == []


class TestAConfiguredServerThatRejectedIsNotAnUnconfiguredOne:

    def test_a_failed_live_send_reports_failed_not_stubbed(self, monkeypatch):
        """
        THE BUG. A degraded live failure sets `stubbed=True` and `failed=True`,
        and the dispatch endpoint tested `stubbed` first — so the screen said
        "no outbound mail server is configured" about a deployment that had
        one, and the SMTP error reached nobody.
        """
        cfg = ActionAgentConfig(smtp_host="smtp.example.com", email_strict=False)
        sender = EmailSender(cfg)
        monkeypatch.setattr(
            EmailSender, "_send_live",
            lambda self, **kw: (_ for _ in ()).throw(RuntimeError("auth failed")))

        result = sender.send(to=["a@x.com"], subject="s", body="b")

        assert result.stubbed is True      # unchanged: it degraded to a stub
        assert result.failed is True
        assert result.outcome == "failed"  # and it is REPORTED as what it was
        assert "auth failed" in result.notes

    def test_no_credential_configured_is_still_stubbed(self, unconfigured):
        result = EmailSender(unconfigured).send(to=["a@x.com"], subject="s",
                                                body="b")
        assert result.outcome == "stubbed"

    def test_the_endpoint_reads_the_verdict_rather_than_rebuilding_it(self):
        """
        The precedence lives on the result now. Rebuilt at a call site it can
        be, and was, rebuilt differently.
        """
        api = (pathlib.Path(__file__).resolve().parents[3] / "app" / "backend"
               / "api" / "actions.py").read_text(encoding="utf-8")
        assert "result=result.outcome," in api
        assert '"stubbed" if result.stubbed else' not in api


class TestAnOperatorCanSeeThisBeforeSomebodyPressesSend:

    def test_the_sender_describes_itself(self, unconfigured):
        state = EmailSender(unconfigured).describe()
        assert state["channel"] == "none"
        assert state["configured"] is False
        # The variable is named HERE, for the person who can set it.
        assert "NETGRAVITY_SMTP_HOST" in state["reason"]

    def test_a_configured_sender_says_so(self):
        state = EmailSender(ActionAgentConfig(
            smtp_host="smtp.example.com", smtp_username="u")).describe()
        assert state["configured"] is True
        assert state["channel"] == "smtp"

    def test_an_unauthenticated_relay_is_stated_not_refused(self):
        """
        Plenty of internal relays accept mail from inside the network with no
        login. Refusing that would break a legitimate deployment; saying
        nothing would hide the commonest misconfiguration of a hosted one.
        """
        state = EmailSender(ActionAgentConfig(smtp_host="relay.internal")).describe()
        assert state["configured"] is True
        assert "NETGRAVITY_SMTP_USERNAME" in state["reason"]

    def test_it_never_reports_a_credential(self):
        state = EmailSender(ActionAgentConfig(
            smtp_host="smtp.example.com", smtp_username="u",
            smtp_password="hunter2")).describe()
        assert "hunter2" not in repr(state)

    def test_the_status_endpoint_carries_it(self):
        app_py = (pathlib.Path(__file__).resolve().parents[3] / "app" / "backend"
                  / "app.py").read_text(encoding="utf-8")
        assert '"outbound_email": _outbound_email_status()' in app_py


class TestTheScreenSaysItInTheReadersOwnTerms:

    def test_no_environment_variable_is_named_at_a_planner(self, insight_js):
        """
        "Set NETGRAVITY_SMTP_HOST to send for real" — an instruction addressed
        to somebody who is not in the room, on a screen read by whoever owns
        the network rather than whoever deploys it.
        """
        block = insight_js[insight_js.index("function deliveryNoteHtml()"):]
        block = block[:block.index("\n}\n")]
        assert "NETGRAVITY_" not in block

    def test_a_request_was_addressed_to_someone_not_from_them(self, insight_js):
        block = insight_js[insight_js.index("function outcomeHtml()"):]
        block = block[:block.index("\n}\n")]
        assert "Already sent to " in block
        assert "Already saved for " in block
        assert "from ${insdEsc" not in block

    def test_the_verb_matches_the_outcome(self, insight_js):
        """
        Measured: "Already sent to X today at 20:17. It was saved but not
        delivered." The sentence took its own first clause back, and a reader
        who stopped at the full stop had been told something false.
        """
        block = insight_js[insight_js.index("function outcomeHtml()"):]
        block = block[:block.index("\n}\n")]
        stub = block[block.index("previous.result === 'stubbed'"):]
        stub = stub[:stub.index("previous.result === 'failed'")]
        assert "Already saved for" in stub
        assert "Already sent" not in stub

    def test_no_internal_enum_reaches_the_sentence(self, insight_js):
        """The screenshot read "... (stubbed)." — a Python string literal."""
        block = insight_js[insight_js.index("function outcomeHtml()"):]
        block = block[:block.index("\n}\n")]
        assert "(${insdEsc(previous.result" not in block

    def test_a_partial_send_names_who_was_not_asked(self, insight_js):
        block = insight_js[insight_js.index("function outcomeHtml()"):]
        block = block[:block.index("\n}\n")]
        assert "outcome.delivery === 'partial'" in block
        assert "they have not been asked" in block

    def test_the_button_does_not_promise_a_send_that_cannot_happen(self, insight_js):
        block = insight_js[insight_js.index("function requestPanelHtml(item)"):]
        block = block[:block.index("\n}\n")]
        assert "EMAIL_DELIVERY.mode === 'stub'" in block
        assert "'Save request'" in block

    def test_the_timestamp_is_a_moment_a_person_can_place(self, insight_js):
        assert "function insdWhen(" in insight_js
        block = insight_js[insight_js.index("function insdWhen("):]
        block = block[:block.index("\n}\n")]
        assert "toLocaleTimeString" in block
        assert "today at" in block


# ---------------------------------------------------------------------------
# The recommendation, in the order a reader needs it
# ---------------------------------------------------------------------------

class TestTheRecommendationLeadsWithTheAnswer:

    def test_there_is_a_summary_before_any_prose(self, scenarios_js):
        assert "function atAGlanceHtml(scn, comparison)" in scenarios_js

    def test_it_answers_the_four_questions_a_reader_has(self, scenarios_js):
        block = scenarios_js[scenarios_js.index("function atAGlanceHtml(scn, comparison)"):]
        block = block[:block.index("\n}\n")]
        for label in ("'You changed'", "'Cost'", "'Of which'", "'Service'",
                      "'Capacity'"):
            assert label in block, label

    def test_every_figure_in_it_is_the_backends(self, scenarios_js):
        """
        §9. The summary reads the ranking, the authoritative KPI values and the
        capacity block; it must not derive a business value of its own.
        """
        block = scenarios_js[scenarios_js.index("function atAGlanceHtml(scn, comparison)"):]
        block = block[:block.index("\n}\n")]
        assert "readKpiValue(scn.scenarioKpis, 'business_network_cost')" in block
        assert "comparison.attribution" in block
        assert "scn.capacityResponse" in block
        # No arithmetic on money: the delta is the backend's own `cost_delta`.
        assert "row.cost_delta" in block
        assert "cost -" not in block and "- cost" not in block

    def test_an_unavailable_figure_is_left_out_not_zeroed(self, scenarios_js):
        block = scenarios_js[scenarios_js.index("function atAGlanceHtml(scn, comparison)"):]
        block = block[:block.index("\n}\n")]
        assert "typeof cost === 'number'" in block
        assert "if (!rows.length) return '';" in block

    def test_the_change_is_described_from_the_request_not_the_result(
            self, scenarios_js):
        """
        "What did I ask for" and "what did the solver do with it" are two
        questions. The card answers the second one everywhere else.
        """
        block = scenarios_js[scenarios_js.index("function requestSummary(scn)"):]
        block = block[:block.index("\n}\n")]
        assert "scn.request" in block
        assert "demand_multiplier" in block
        assert "demand_product_category" in block

    def test_an_uploaded_name_cannot_inject_markup(self, scenarios_js):
        block = scenarios_js[scenarios_js.index("function requestSummary(scn)"):]
        block = block[:block.index("\n}\n")]
        assert "const esc = (t) =>" in block

    def test_the_attribution_is_stated_once(self, scenarios_js):
        """
        It was on the card three times: a paragraph, the backend's own sentence
        in the collapsed detail, and now the summary's "Of which" row.
        """
        assert "function attributionHtml(" not in scenarios_js
        assert "'Of which'" in scenarios_js


class TestOneFindingIsPrintedOnce:
    """
    Measured on the first briefing the gateway actually wrote for this network:
    the headline was the meaning's first 140 characters with an ellipsis, and
    the card printed both. One finding, arriving as two.
    """

    def test_a_headline_that_is_only_the_openings_of_its_body_is_dropped(
            self, scenarios_js):
        block = scenarios_js[scenarios_js.index("function narrativeHtml("):]
        block = block[:block.index("\n}\n")]
        assert "meaning.trim().toLowerCase().startsWith(stem)" in block
        # The body is kept, not the truncated copy: the longer text contains
        # the shorter one, so nothing is lost.
        assert "headline = ''" in block

    def test_a_genuinely_different_headline_survives(self, scenarios_js):
        block = scenarios_js[scenarios_js.index("function narrativeHtml("):]
        block = block[:block.index("\n}\n")]
        assert "if (headline && meaning) {" in block
        assert "if (!headline && !meaning) return '';" in block


class TestAWhatIfLeadsWithWhatTheChangeDid:

    def test_the_scenario_insight_comes_before_the_baseline_cost(self):
        """
        `card_from_briefing` leads with `kpi_insights[0]`, and on a what-if
        that was the Cost insight — "The cost this network runs at today",
        ending "the decision baseline for comparing any scenario", printed
        under the scenario's own name.
        """
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        result = ReasoningAgent(None, runtime=None).reason({
            "network_state": {"business_network_cost": 1000.0},
            "scenario": {"business_cost_delta": 120.0,
                         "business_cost_delta_pct": 12.0},
        })
        themes = [i.theme for i in result.briefing.kpi_insights]
        assert themes[0] == "Scenario impact"
        assert "Cost" in themes          # not removed, just no longer leading

    def test_a_network_run_still_leads_with_its_cost(self):
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        result = ReasoningAgent(None, runtime=None).reason(
            {"network_state": {"business_network_cost": 1000.0}})
        assert result.briefing.kpi_insights[0].theme == "Cost"

    def test_the_scenario_headline_survives_having_the_first_person_removed(self):
        """
        It was "I see business cost increases versus baseline". The card strips
        the first person from everything a reader sees, which left a fragment
        beginning lowercase as the first line on the screen — the same defect
        already fixed once on the Cost headline.
        """
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        from netgravity.orchestrator.reasoning.card import clean

        result = ReasoningAgent(None, runtime=None).reason({
            "network_state": {"business_network_cost": 1000.0},
            "scenario": {"business_cost_delta": 120.0},
        })
        headline = result.briefing.kpi_insights[0].headline
        assert not headline.lower().startswith("i ")
        shown = clean(headline, 140)
        assert shown[:1].isupper(), shown
        assert len(shown.split()) >= 4, shown
