"""
Market intelligence through the orchestrator — the chat route and the
document route, and the boundary that keeps both away from the risk chain.

EVERY TEST HERE RUNS OFFLINE. `allow_llm=False` and stub-mode config are set
explicitly, so this file behaves the same with or without credentials in the
environment.

The recurring subject is one distinction: a market change has a MAGNITUDE and
has already happened; a hazard has a PROBABILITY and might. Confusing them in
either direction is a correctness failure, not a taxonomy quibble —
`RF = P + REI - P*REI` is computed from a probability, and governance is
decided from RF.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.orchestrator.agents.extraction_agent import ExtractionParsingAgent
from netgravity.orchestrator.agents.intent_agent import IntentAgent
from netgravity.orchestrator.conversation.nlu import ConversationalNLU
from netgravity.orchestrator.core.planner import WORKFLOW_TEMPLATES
from netgravity.orchestrator.schemas.conversation import MarketSignalSpec
from netgravity.orchestrator.schemas.extraction import (
    ExtractionRequest,
    ExtractionStatus,
    SourceType,
)
from netgravity.orchestrator.schemas.requests import Intent

from netgravity.tests.integration.conftest import build_delhi_network


@pytest.fixture
def network():
    return build_delhi_network()


@pytest.fixture
def nlu():
    return ConversationalNLU()


def _understand(nlu, network, text: str):
    """One turn, offline. No gateway is consulted."""
    return nlu.understand(text, network, allow_llm=False)


# ---------------------------------------------------------------------------
# The chat route
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Diesel is up 6% this week.",
    "Our carrier has hiked trucking rates by 12%.",
    "The government has increased customs duty on imports.",
    "Port handling charges at Mumbai went up 5% in January.",
])
def test_a_reported_market_change_is_market_intelligence(nlu, network, text):
    assert _understand(nlu, network, text).intent == Intent.MARKET_INTELLIGENCE


@pytest.mark.parametrize("text", [
    "Flooding is expected around DC_DELHI.",
    "A cyclone warning has been issued for Mumbai.",
    "There is a 70% probability of flooding around DC_DELHI.",
    "Workers at DC_MUMBAI have announced a strike.",
])
def test_a_hazard_is_not_stolen_by_the_market_rule(nlu, network, text):
    """
    The reason the market rule requires a SUBJECT and a CHANGE together.

    A single-keyword market rule placed before the hazard rule would swallow
    "flooding is expected"; placed after it, the hazard rule's own vocabulary
    ("expected", "warning", "alert") would swallow every forward-looking price
    story. The compound test is what lets market run first without being able
    to take a hazard: "flooding" is not a market subject.
    """
    assert _understand(nlu, network, text).intent == Intent.EXTERNAL_EVENT


def test_a_forward_looking_price_story_is_not_a_forecast_request(nlu, network):
    """
    "Fuel prices are expected to rise 8% next month" contains projection
    vocabulary twice over and is still market intelligence.

    Routed as FORECAST it would reach a workflow with no engine, be declined
    honestly, and lose a real signal at the door.
    """
    result = _understand(nlu, network, "Fuel prices are expected to rise 8% next month.")
    assert result.intent == Intent.MARKET_INTELLIGENCE


@pytest.mark.parametrize("text", [
    "What has diesel done this year?",
    "How much did freight rates increase?",
])
def test_a_question_about_prices_is_not_a_reported_change(nlu, network, text):
    """
    Nothing is stated, so there is nothing to record.

    Treating a question as a signal would invent a fact from a query — the
    system would end up holding "diesel changed" as evidence because someone
    asked whether it had.
    """
    assert _understand(nlu, network, text).intent != Intent.MARKET_INTELLIGENCE


def test_the_spec_records_what_was_said_not_a_parsed_number(nlu, network):
    result = _understand(nlu, network, "Diesel is up 6% this week.")
    signal = result.market_signal

    assert isinstance(signal, MarketSignalSpec)
    assert signal.direction == "UP"
    assert signal.subject == "diesel"
    assert signal.bucket == "MACRO"
    # The user's own words. Not 0.06, and not a float in unspecified units one
    # assignment away from a solver input.
    assert signal.magnitude == "6%"
    assert isinstance(signal.magnitude, str)


def test_the_spec_cannot_hold_a_probability():
    """
    Structural, not behavioural. There is no field to populate, so no future
    change to a prompt or a parser can start filling one in.
    """
    assert "event_probability" not in MarketSignalSpec.model_fields
    banned = {"probability", "likelihood", "chance", "p", "risk"}
    assert not banned & set(MarketSignalSpec.model_fields)


def test_no_effective_date_is_invented(nlu, network):
    """An undated statement stays undated. Today's date is not a default."""
    result = _understand(nlu, network, "Diesel is up 6%.")
    assert result.market_signal.effective_date is None


def test_a_hazard_turn_produces_no_market_signal(nlu, network):
    result = _understand(nlu, network, "Flooding is expected around DC_DELHI.")
    assert result.market_signal is None
    assert result.external_event is not None


def test_a_market_turn_produces_no_external_event(nlu, network):
    result = _understand(nlu, network, "Diesel is up 6% this week.")
    assert result.external_event is None
    assert result.market_signal is not None


def test_naming_a_site_resolves_it_rather_than_asking_which_one(nlu, network):
    """
    "Mumbai" is not a clarification question here.

    A market signal is about the outside world; asking "which Mumbai did you
    mean?" would interrupt a statement with a question about a node the
    sentence was not really about. Whether the change touches DC_MUMBAI is
    settled afterwards, deterministically, by the guardrail.
    """
    result = _understand(nlu, network,
                         "Port handling charges at Mumbai went up 5% in January.")
    assert result.intent == Intent.MARKET_INTELLIGENCE
    assert result.clarification is None
    assert "DC_MUMBAI" in result.resolved_entity_ids


# ---------------------------------------------------------------------------
# The workflow it lands in
# ---------------------------------------------------------------------------

def test_the_intent_has_a_workflow():
    assert Intent.MARKET_INTELLIGENCE in WORKFLOW_TEMPLATES


def test_the_workflow_neither_solves_nor_creates_a_scenario():
    """
    A news item is evidence, not an instruction.

    Re-optimising on a headline would answer a question nobody asked against
    inputs nobody changed — every rate in the snapshot is still the contracted
    rate — and editing those rates first would be a model changing a number
    the MILP treats as fact.
    """
    from netgravity.orchestrator.schemas.requests import IntentResolution

    template = WORKFLOW_TEMPLATES[Intent.MARKET_INTELLIGENCE]
    steps = template.build(IntentResolution(intent=Intent.MARKET_INTELLIGENCE))
    capabilities = {s.capability for s in steps}

    assert not any("optimi" in c.lower() for c in capabilities)
    assert not any("scenario" in c.lower() for c in capabilities)
    # Governance still runs. Every response leaves with a verdict.
    assert any("govern" in c.lower() for c in capabilities)


# ---------------------------------------------------------------------------
# The document route through the Extraction / Parsing Agent
# ---------------------------------------------------------------------------

def test_a_document_lands_in_market_intelligence_not_external_signals(tmp_path):
    """
    Two lists, and nothing crosses between them.

    A single mixed list would force every consumer to type-test its contents
    to learn whether it held a hazard carrying a probability or a price change
    that must never appear to. Getting that test wrong is the failure the
    Phase 4A rename was performed to prevent.
    """
    article = tmp_path / "diesel.txt"
    article.write_text(
        "Diesel prices were raised by 6% on 15 January 2026.", encoding="utf-8")

    result = ExtractionParsingAgent().extract(ExtractionRequest(
        source=str(article),
        source_type=SourceType.MARKET_INTELLIGENCE_DOC,
        options={"known_facility_ids": ["DC_DELHI"]},
    ))

    assert result.status in (ExtractionStatus.ACCEPTED, ExtractionStatus.WARNING)
    assert result.market_intelligence
    assert result.external_signals == []
    assert result.canonical_data is None, "a signal is not network master data"


def test_an_unreadable_document_is_rejected_not_guessed(tmp_path):
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"%PDF-1.4\n%%EOF\n")

    result = ExtractionParsingAgent().extract(ExtractionRequest(
        source=str(scan), source_type=SourceType.MARKET_INTELLIGENCE_DOC,
    ))

    assert result.market_intelligence == []
    assert result.status in (ExtractionStatus.WARNING, ExtractionStatus.REJECTED)


def test_the_two_document_routes_are_separate_source_types():
    """
    `EXTERNAL_SIGNAL_TEXT` reads a hazard; `MARKET_INTELLIGENCE_DOC` reads a
    market change. Reusing one for the other would either present a known cost
    change as a disruption of unknown likelihood, or silently discard the
    probability the risk chain needs.
    """
    assert SourceType.EXTERNAL_SIGNAL_TEXT != SourceType.MARKET_INTELLIGENCE_DOC


def test_extraction_still_cannot_carry_an_engine_owned_value():
    """The Phase 4A boundary, re-asserted now that a new field exists."""
    from netgravity.orchestrator.schemas.extraction import ExtractionResult

    banned = {"rei", "rf", "risk_factor", "governance", "objective",
              "event_probability", "probability", "scenario_overrides"}
    assert not banned & set(ExtractionResult.model_fields)


# ---------------------------------------------------------------------------
# The chat route actually persists what it recognises
# ---------------------------------------------------------------------------
#
# Recognition alone was a real gap: the NLU built a `MarketSignalSpec`, but
# nothing carried it past the translate step — `OrchestratorRequest` had no
# field for it, so it existed only for the instant inside `understand()` and
# was then discarded. These tests exercise the full chat() round trip, which
# is the only way to prove the signal genuinely reaches the audit trace rather
# than merely typechecking a schema.

from datetime import datetime, timezone

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.conversation.chat_service import ChatService
from netgravity.orchestrator.schemas.conversation import ChatRequest


@pytest.fixture
def chat_orch():
    """A fully wired orchestrator, LLM disabled — same fixture pattern the
    existing conversational-workflow tests use."""
    return build_orchestrator(network=build_delhi_network(), enable_llm=False)


@pytest.fixture
def chat(chat_orch):
    return ChatService(chat_orch)


def _say(chat, message: str):
    return chat.chat(ChatRequest(message=message, disable_llm=True))


def test_a_typed_signal_reaches_the_audit_trace(chat, chat_orch):
    """
    The gap, closed. Before this, `ctx.market_signal` never existed:
    `OrchestratorRequest` had no field for it and `ChatTurn` never recorded
    one — recognition and persistence were two different things, and only the
    first one worked.
    """
    response = _say(chat, "Diesel is up 6% this week.")

    trace = chat_orch.audit.get(response.execution_id)
    scored = trace.engine_results.get("market.score_signal")

    assert scored is not None, "the signal never reached the audit trace"
    assert scored["magnitude"] == "6%"
    assert scored["bucket"] == "MACRO"
    assert scored["direction"] == "UP"
    # The actual guardrail ran against the actual network — not a stand-in.
    assert scored["verdict"] is not None


def test_published_date_is_the_moment_of_the_message(chat, chat_orch):
    """
    The explicit exception agreed for chat: "right now" is not a guess here,
    it is the fact — there is no other candidate date for a typed sentence,
    unlike a document, which `adapters/market_intelligence.py` REJECTS for
    lacking a stated one (R-029). A chat message has no source to be wrong
    about; the moment it arrived is itself an observed fact.
    """
    before = datetime.now(timezone.utc).date().isoformat()
    response = _say(chat, "Diesel is up 6% this week.")
    after = datetime.now(timezone.utc).date().isoformat()

    trace = chat_orch.audit.get(response.execution_id)
    scored = trace.engine_results["market.score_signal"]
    assert before <= scored["published_date"] <= after
    # No date was assumed for anything else — the user never stated one.
    assert scored["effective_date"] is None


def test_chat_signals_default_to_low_confidence(chat, chat_orch):
    """
    One notch more conservative than the document/spreadsheet MEDIUM default.
    Those name a source that could in principle be checked; a typed sentence
    names none.
    """
    response = _say(chat, "Our carrier has hiked trucking rates by 12%.")
    trace = chat_orch.audit.get(response.execution_id)
    scored = trace.engine_results["market.score_signal"]
    assert scored["confidence"] == "LOW"


def test_no_probability_field_reaches_the_recorded_signal(chat, chat_orch):
    response = _say(chat, "Diesel is up 6% this week.")
    trace = chat_orch.audit.get(response.execution_id)
    scored = trace.engine_results["market.score_signal"]
    assert "event_probability" not in scored
    assert "probability" not in scored


def test_a_hazard_message_still_produces_external_signal_not_market(
        chat, chat_orch):
    """Regression: the two paths must not cross now that both are wired up."""
    response = _say(chat, "Flooding is expected around DC_DELHI.")
    trace = chat_orch.audit.get(response.execution_id)
    assert "market.score_signal" not in trace.engine_results
    assert "external.interpret_signal" in trace.engine_results


def test_a_market_message_never_produces_external_signal(chat, chat_orch):
    response = _say(chat, "Diesel is up 6% this week.")
    trace = chat_orch.audit.get(response.execution_id)
    assert "external.interpret_signal" not in trace.engine_results


def test_the_reply_never_states_an_ungrounded_number(chat):
    """
    The magnitude, the guardrail's relevance score and its threshold are all
    real numbers, and none of them were computed or verified by the MILP, KPI,
    REI or risk engine — the only sources the numeric-claim validator
    recognises as authoritative. Putting any of them in free narrative text
    gets them stripped as UNSUPPORTED, which is the validator doing its job
    correctly, not a bug to work around.

    So the narrative states the signal qualitatively (category, direction,
    guardrail outcome) and points at the recorded signal for the figure
    itself, the same way the reasoning agent already handles a hazard with no
    defensible probability — by naming what is known and not inventing text
    that LOOKS like a verified number.
    """
    response = _say(chat, "Diesel is up 6% this week.")
    assert "UNSUPPORTED" not in response.reply
    assert "REMOVED" not in response.reply
    assert "market signal" in response.reply.lower()


def test_the_reply_reflects_whether_the_signal_cleared_the_guardrail(chat):
    filtered = _say(chat, "Diesel is up 6% this week.")
    passed = _say(chat, "Port handling charges at Mumbai went up 5% in January.")
    assert "did not" in filtered.reply.lower() or "not clear" in filtered.reply.lower()
    assert "cleared" in passed.reply.lower()


def test_the_workflow_still_governs_a_market_intelligence_turn(chat):
    """Every response leaves with a verdict, market signals included."""
    response = _say(chat, "Diesel is up 6% this week.")
    assert response.governance is not None
