"""
Orchestrator — Chat service.

The conversational front door:

    ChatRequest
        → NLU                      understand
        → validated intent          (schema boundary)
        → OrchestratorRequest       translate
        → Orchestrator.run()        DECIDE and execute
        → ChatResponse              format

WHAT THIS MODULE IS NOT ALLOWED TO DO, and how that is enforced
───────────────────────────────────────────────────────────────
It does not choose a workflow. It hands the orchestrator an `Intent` enum value
and `WorkflowPlanner` maps that to a graph. There is no branch here that names a
capability, and a test asserts as much by reading this file's source.

It does not compute. Every figure in a reply is read from a `ReasoningResult`
that has ALREADY passed numeric grounding, or from a deterministic result block
verbatim. The formatter has no arithmetic in it beyond `len()` on a list of
facilities — a count of things the snapshot already contains.

It does not bypass governance. The verdict comes back on the `FinalResponse` and
is surfaced, never overridden. A user typing "close Delhi" produces an intent, a
workflow, and a governance decision; it never produces a closure.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from netgravity.orchestrator.audit import events
from netgravity.orchestrator.conversation.entity_resolver import EntityResolver
from netgravity.orchestrator.conversation.nlu import ConversationalNLU
from netgravity.orchestrator.conversation.store import ConversationStore
from netgravity.orchestrator.exceptions import LLMFailureError, OrchestratorError
from netgravity.orchestrator.schemas.actions import FinalResponse
from netgravity.orchestrator.schemas.conversation import (
    INTENT_SCHEMA_VERSION,
    AmbiguityKind,
    ChatRequest,
    ChatResponse,
    ChatTurn,
    ConversationalIntent,
    IntentClarity,
)
from netgravity.orchestrator.schemas.requests import (
    ExternalSignal,
    Intent,
    OrchestratorRequest,
)
from netgravity.schemas.network import NodeRole

logger = logging.getLogger(__name__)

#: Intents whose answers describe a HYPOTHETICAL network.
_SCENARIO_INTENTS = {Intent.SCENARIO_ANALYSIS, Intent.SCENARIO_COMPARISON}


class ChatService:
    """
    Conversational interface over an existing `Orchestrator`.

    Construct with a live orchestrator; the chat layer owns no engines and no
    state beyond conversation history.
    """

    def __init__(
        self,
        orchestrator: Any,
        *,
        nlu: Optional[ConversationalNLU] = None,
        store: Optional[ConversationStore] = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.store = store or ConversationStore()
        self.nlu = nlu or ConversationalNLU(
            intent_agent=orchestrator.services.get("intent_agent"),
            signal_agent=orchestrator.services.get("signal_agent"),
        )

    # ==================================================================
    # Entry point
    # ==================================================================

    def chat(self, request: ChatRequest) -> ChatResponse:
        """
        Handle one user message.

        Never raises: a conversational surface that throws loses the thread. All
        failures become a controlled reply with the reason stated.
        """
        started = time.perf_counter()
        conversation = self.store.start(request.conversation_id)
        cid = conversation.conversation_id
        turn_id = str(uuid.uuid4())[:12]

        logger.info(
            "chat.request_received conversation_id=%s turn_id=%s chars=%d",
            cid, turn_id, len(request.message or ""),
        )

        snapshot_id = request.network_snapshot_id or self.orchestrator.snapshots.current_id
        if snapshot_id is None:
            return self._controlled_failure(
                cid, turn_id,
                "No network snapshot is registered, so there is nothing to answer "
                "questions about.",
                started,
            )
        conversation.network_snapshot_id = (
            conversation.network_snapshot_id or snapshot_id
        )
        snapshot = self.orchestrator.snapshots.get(snapshot_id)

        # ---- UNDERSTAND -------------------------------------------------
        try:
            intent = self.nlu.understand(
                request.message,
                snapshot.network,
                conversation_id=cid,
                allow_llm=not request.disable_llm,
                prior_entity_ids=conversation.last_entity_ids,
                prior_intent=conversation.last_intent,
                # Structured, bounded, and carrying no scenario override and no
                # result value. See `ConversationContext` for why this is a
                # schema rather than a pasted transcript.
                context=conversation.context(
                    available_entity_ids=[
                        f.id for f in snapshot.network.facilities
                        if f.role.value not in ("MARKET", "CUSTOMER")
                    ],
                ),
            )
        except LLMFailureError as exc:
            logger.warning("chat.llm_failure conversation_id=%s code=%s",
                           cid, exc.code.value)
            return self._controlled_failure(
                cid, turn_id,
                f"I could not interpret that request: the language model is "
                f"unavailable ({exc.code.value}). No intent was assumed. "
                f"Deterministic results are unaffected — you can retry, or phrase "
                f"the request using an exact facility id.",
                started, error_code=exc.code.value,
            )
        except Exception as exc:  # noqa: BLE001 — schema/validation refusal
            # A ValidationError here means the model tried to assert something
            # the schema forbids (a cost, an REI, an RF). Refusing loudly is the
            # correct outcome and it must appear in the record.
            logger.warning("chat.intent_validation_failed conversation_id=%s error=%s",
                           cid, exc)
            return self._controlled_failure(
                cid, turn_id,
                f"That request could not be turned into a valid instruction: "
                f"{type(exc).__name__}. Nothing was executed.",
                started, error_code="INTENT_VALIDATION_FAILED",
            )

        logger.info(
            "chat.intent_classified conversation_id=%s intent=%s clarity=%s "
            "source=%s entities=%s",
            cid, intent.intent.value, intent.clarity.value, intent.source,
            intent.resolved_entity_ids,
        )

        # ---- DETERMINISTIC ENTITY GATE -----------------------------------
        # The last point at which an unresolvable node can be stopped. The NLU
        # already refuses these; this is a second, independent check placed
        # where execution actually begins, so a future change to intent routing
        # cannot reopen the hole by accident. Nothing below this line runs for a
        # facility master data does not contain: no MILP, no REI, no RF, no
        # governance.
        if intent.unresolved_mentions:
            logger.warning(
                "chat.entity_gate_refused conversation_id=%s unresolved=%s intent=%s",
                cid, intent.unresolved_mentions, intent.intent.value,
            )
            return self._clarification_response(cid, turn_id, intent, started)

        # ---- CLARIFY (do not guess) --------------------------------------
        if intent.needs_clarification:
            return self._clarification_response(cid, turn_id, intent, started)

        if intent.clarity == IntentClarity.UNSUPPORTED or not intent.is_actionable:
            return self._unsupported_response(cid, turn_id, intent,
                                              snapshot.network, started)

        # ---- intents answered WITHOUT the solver --------------------------
        if intent.intent == Intent.STATUS_QUERY:
            return self._status_response(cid, turn_id, intent, snapshot, started)
        if intent.intent == Intent.FORECAST:
            return self._forecast_response(cid, turn_id, intent, started)

        # ---- TRANSLATE and hand over -------------------------------------
        orchestrator_request = self._to_orchestrator_request(
            intent, request, snapshot_id,
        )
        try:
            final = self.orchestrator.run_sync(orchestrator_request)
        except OrchestratorError as exc:
            return self._controlled_failure(
                cid, turn_id, f"The request could not be executed: {exc.message}",
                started, error_code=exc.code.value,
            )

        response = self._to_chat_response(cid, turn_id, intent, final, started)
        self._record_turn(cid, request, intent, response)
        self._record_chat_events(final.execution_id, cid, turn_id, intent, response)
        return response

    # ==================================================================
    # TRANSLATE — intent → OrchestratorRequest
    # ==================================================================

    def _to_orchestrator_request(
        self,
        intent: ConversationalIntent,
        request: ChatRequest,
        snapshot_id: str,
    ) -> OrchestratorRequest:
        """
        Build the request the orchestrator already understands.

        Note the shape of this method: it passes `explicit_intent` and lets the
        planner decide the workflow. It never names a capability, a step or an
        engine. That is the structural guarantee that the LLM cannot select what
        runs — its influence ends at an enum value.
        """
        external_signal: Optional[ExternalSignal] = None
        event = intent.external_event
        if event is not None:
            external_signal = ExternalSignal(
                event_type=event.event_type,
                location=event.location,
                severity=event.severity,
                # Only ever what the user actually stated. Severity is carried
                # as severity and is never converted into a probability.
                event_probability=event.event_probability,
                probability_basis=event.probability_basis,
                source="user_conversation",
                source_quality="USER_ASSERTED",
                confidence=intent.confidence,
                evidence=request.message[:500],
                affected_entity_ids=list(intent.resolved_entity_ids),
            )

        return OrchestratorRequest(
            request_id=request.request_id or str(uuid.uuid4()),
            input=request.message,
            explicit_intent=intent.intent,
            explicit_scenarios=list(intent.scenario_overrides),
            external_signal=external_signal,
            network_snapshot_id=snapshot_id,
            disable_llm=request.disable_llm,
            metadata={
                "conversation_id": intent.conversation_id or "",
                "intent_schema_version": intent.schema_version,
                "prompt_version": intent.prompt_version or "",
                "intent_source": intent.source,
                "intent_confidence": intent.confidence,
                "resolved_entity_ids": list(intent.resolved_entity_ids),
            },
        )

    # ==================================================================
    # FORMAT — deterministic result → natural language
    # ==================================================================

    def _to_chat_response(
        self,
        conversation_id: str,
        turn_id: str,
        intent: ConversationalIntent,
        final: FinalResponse,
        started: float,
    ) -> ChatResponse:
        provenance = "SCENARIO" if final.is_hypothetical else "OBSERVED"
        reply = self._compose_reply(intent, final, provenance)

        return ChatResponse(
            conversation_id=conversation_id,
            turn_id=turn_id,
            reply=reply,
            intent=intent.intent.value,
            clarity=intent.clarity.value,
            intent_schema_version=intent.schema_version,
            intent_confidence=intent.confidence,
            intent_source=intent.source,
            resolved_entity_ids=list(intent.resolved_entity_ids),
            provenance=provenance,
            execution_id=final.execution_id,
            workflow_id=self._workflow_of(final.execution_id),
            network_snapshot_id=final.network_snapshot_id,
            scenario_id=final.scenario_id,
            status=final.status,
            results=dict(final.results),
            risk=final.risk,
            # mode="json" so enums serialise to their values: this block goes
            # over the wire, and "ActionClassification.HUMAN_ONLY" is not
            # something to hand a client.
            governance=(final.governance.model_dump(mode="json")
                        if final.governance else None),
            grounding_status=(final.reasoning.grounding_status
                              if final.reasoning else None),
            warnings=list(final.warnings),
            errors=list(final.errors),
            duration_seconds=round(time.perf_counter() - started, 4),
        )

    def _compose_reply(
        self,
        intent: ConversationalIntent,
        final: FinalResponse,
        provenance: str,
    ) -> str:
        """
        Assemble the user-visible answer.

        Every figure here comes from `final.reasoning.summary`, which the
        Reasoning Agent produced and numeric grounding already adjudicated. This
        method concatenates and labels; it does not calculate, and it does not
        re-verify — building a second grounding check here would create exactly
        the duplicate system the phase forbids.
        """
        parts: List[str] = []

        if provenance == "SCENARIO":
            parts.append(
                "This is a SCENARIO result — a hypothetical. The observed "
                "network is unchanged."
            )

        if final.reasoning is not None and final.reasoning.summary.strip():
            parts.append(final.reasoning.summary.strip())
        elif final.summary:
            parts.append(final.summary)
        else:
            parts.append("The analysis produced no narrative result.")

        if final.reasoning is not None:
            if final.reasoning.grounding_status == "GROUNDING_FAILED":
                parts.append(
                    "Some figures in the generated explanation could not be "
                    "verified against the authoritative results and were removed."
                )
            if final.reasoning.recommendation.strip():
                parts.append(f"Suggested next step: {final.reasoning.recommendation.strip()}")

        governance = final.governance
        if governance is not None:
            parts.append(self._describe_governance(governance))

        return " ".join(p for p in parts if p)

    @staticmethod
    def _describe_governance(governance: Any) -> str:
        """
        State the action tier in plain language.

        Critically, this distinguishes "we could not establish the facts" from
        "we did, and they are concerning" — the same distinction the governance
        layer records in `blocked_by_missing_evidence`.
        """
        classification = governance.classification.value
        if classification == "HUMAN_ONLY":
            return ("This requires a human decision and cannot be actioned "
                    f"automatically: {governance.reason}")
        if classification == "APPROVAL_REQUIRED":
            if getattr(governance, "blocked_by_missing_evidence", False):
                return ("Autonomous action is withheld because required evidence "
                        "is unavailable — not because measured risk is high. "
                        "A planner should review this.")
            return f"This needs planner approval before any action: {governance.reason}"
        if classification == "NO_ACTION":
            return ""
        return "No action is proposed; this is analysis only."

    def _workflow_of(self, execution_id: Optional[str]) -> Optional[str]:
        """
        Report which workflow the PLANNER chose.

        Read back off the execution, not decided here — this is reporting, not
        routing.
        """
        if execution_id is None:
            return None
        trace = self.orchestrator.audit.get(execution_id)
        return trace.workflow_id if trace is not None else None

    # ==================================================================
    # Non-solver intents
    # ==================================================================

    def _status_response(
        self,
        conversation_id: str,
        turn_id: str,
        intent: ConversationalIntent,
        snapshot: Any,
        started: float,
    ) -> ChatResponse:
        """
        Answer an inventory question straight from the digital twin.

        Counts of facilities are properties of the snapshot, not of an optimum,
        so no solver runs. Anything requiring cost or service is classified
        NETWORK_STATE_QUERY instead and does solve.
        """
        network = snapshot.network
        resolver = EntityResolver(network)
        counts = {
            role.value: resolver.facilities_of_role(role)
            for role in (NodeRole.PLANT, NodeRole.DC, NodeRole.MARKET)
        }
        reply = (
            f"From the current network snapshot ({snapshot.snapshot_id}): "
            f"{len(counts['DC'])} distribution centres "
            f"({', '.join(counts['DC']) or 'none'}), "
            f"{len(counts['PLANT'])} plants, and "
            f"{len(counts['MARKET'])} demand markets. "
            f"These are observed counts read from the digital twin; no "
            f"optimization was run to produce them."
        )
        response = ChatResponse(
            conversation_id=conversation_id,
            turn_id=turn_id,
            reply=reply,
            intent=intent.intent.value,
            clarity=intent.clarity.value,
            intent_confidence=intent.confidence,
            intent_source=intent.source,
            resolved_entity_ids=list(intent.resolved_entity_ids),
            provenance="OBSERVED",
            workflow_id="wf_status",
            network_snapshot_id=snapshot.snapshot_id,
            status="COMPLETED",
            results={"facilities": counts, "data_version": snapshot.data_version},
            duration_seconds=round(time.perf_counter() - started, 4),
        )
        self._record_turn_minimal(conversation_id, intent, response)
        return response

    def _forecast_response(
        self,
        conversation_id: str,
        turn_id: str,
        intent: ConversationalIntent,
        started: float,
    ) -> ChatResponse:
        """
        Recognised, and honestly refused.

        NetGravity has no forecasting engine. Producing a projection here would
        mean inventing one, which is precisely the fabrication this architecture
        exists to prevent — so the request is understood and declined.
        """
        response = ChatResponse(
            conversation_id=conversation_id,
            turn_id=turn_id,
            reply=(
                "I understand you are asking for a forecast, but NetGravity has "
                "no forecasting capability registered. I will not produce a "
                "projection, because any number I gave you would be invented "
                "rather than computed. I can report the observed network state, "
                "run a what-if scenario, or assess resilience exposure."
            ),
            intent=intent.intent.value,
            clarity=intent.clarity.value,
            intent_confidence=intent.confidence,
            intent_source=intent.source,
            provenance="OBSERVED",
            workflow_id="wf_forecast",
            status="COMPLETED",
            warnings=["No forecasting capability is registered."],
            duration_seconds=round(time.perf_counter() - started, 4),
        )
        self._record_turn_minimal(conversation_id, intent, response)
        return response

    # ==================================================================
    # Clarification and failure
    # ==================================================================

    def _clarification_response(
        self,
        conversation_id: str,
        turn_id: str,
        intent: ConversationalIntent,
        started: float,
    ) -> ChatResponse:
        assert intent.clarification is not None
        logger.info(
            "chat.clarification_required conversation_id=%s kind=%s",
            conversation_id, intent.ambiguity.value,
        )
        response = ChatResponse(
            conversation_id=conversation_id,
            turn_id=turn_id,
            reply=intent.clarification.question,
            intent=intent.intent.value,
            clarity=intent.clarity.value,
            intent_confidence=intent.confidence,
            intent_source=intent.source,
            resolved_entity_ids=list(intent.resolved_entity_ids),
            clarification=intent.clarification,
            provenance="OBSERVED",
            status="AWAITING_CLARIFICATION",
            duration_seconds=round(time.perf_counter() - started, 4),
        )
        self._record_turn_minimal(conversation_id, intent, response)
        return response

    def _unsupported_response(
        self,
        conversation_id: str,
        turn_id: str,
        intent: ConversationalIntent,
        network: Any,
        started: float,
    ) -> ChatResponse:
        resolver = EntityResolver(network)
        dcs = resolver.facilities_of_role(NodeRole.DC)
        response = ChatResponse(
            conversation_id=conversation_id,
            turn_id=turn_id,
            reply=(
                "I could not work out what you would like me to do. I can report "
                "the current network state, run what-if scenarios (closing a "
                "facility, changing capacity), assess resilience exposure, or "
                "combine an external event probability with exposure into a risk "
                f"factor. Known distribution centres: {', '.join(dcs) or 'none'}."
            ),
            intent=Intent.UNKNOWN.value,
            clarity=intent.clarity.value,
            intent_confidence=intent.confidence,
            intent_source=intent.source,
            provenance="OBSERVED",
            status="UNSUPPORTED",
            duration_seconds=round(time.perf_counter() - started, 4),
        )
        self._record_turn_minimal(conversation_id, intent, response)
        return response

    def _controlled_failure(
        self,
        conversation_id: str,
        turn_id: str,
        message: str,
        started: float,
        *,
        error_code: str = "CHAT_FAILURE",
    ) -> ChatResponse:
        """
        A failure the user can act on.

        Never fabricates an intent to keep the conversation moving — an invented
        interpretation would be executed against real engines.
        """
        return ChatResponse(
            conversation_id=conversation_id,
            turn_id=turn_id,
            reply=message,
            intent=Intent.UNKNOWN.value,
            clarity=IntentClarity.UNSUPPORTED.value,
            provenance="OBSERVED",
            status="FAILED",
            errors=[{"code": error_code, "message": message}],
            duration_seconds=round(time.perf_counter() - started, 4),
        )

    # ==================================================================
    # Recording
    # ==================================================================

    def _record_turn(
        self,
        conversation_id: str,
        request: ChatRequest,
        intent: ConversationalIntent,
        response: ChatResponse,
    ) -> None:
        self.store.append(conversation_id, ChatTurn(
            turn_id=response.turn_id,
            user_input=request.message,
            intent=intent.intent,
            clarity=intent.clarity,
            resolved_entity_ids=list(intent.resolved_entity_ids),
            execution_id=response.execution_id,
            provenance=response.provenance,
            scenario_id=response.scenario_id,
            # The LABEL only. `ChatTurn` still has no field able to hold a
            # ScenarioIntentSpec, so a later turn can be told what was asked
            # and cannot silently re-apply it.
            scenario_label=(intent.scenario_overrides[0].label
                            if intent.scenario_overrides else None),
            reply=response.reply,
        ))

    def _record_turn_minimal(
        self,
        conversation_id: str,
        intent: ConversationalIntent,
        response: ChatResponse,
    ) -> None:
        self.store.append(conversation_id, ChatTurn(
            turn_id=response.turn_id,
            user_input="",
            intent=intent.intent,
            clarity=intent.clarity,
            resolved_entity_ids=list(intent.resolved_entity_ids),
            provenance=response.provenance,
            reply=response.reply,
        ))

    def _record_chat_events(
        self,
        execution_id: Optional[str],
        conversation_id: str,
        turn_id: str,
        intent: ConversationalIntent,
        response: ChatResponse,
    ) -> None:
        """
        Attach conversational provenance to the EXISTING execution trace.

        Deliberately not a second audit system: the same `ExecutionTrace` that
        already records the plan, the engines and the governance verdict gains
        the chat events, so one record answers "why did NetGravity say this?"
        end to end.
        """
        if execution_id is None:
            return
        trace = self.orchestrator.audit.get(execution_id)
        if trace is None:
            return

        trace.record(
            events.CHAT_REQUEST_RECEIVED,
            conversation_id=conversation_id, turn_id=turn_id,
        )
        trace.record(
            events.INTENT_CLASSIFIED,
            conversation_id=conversation_id, turn_id=turn_id,
            intent=intent.intent.value, clarity=intent.clarity.value,
            confidence=intent.confidence, source=intent.source,
            intent_schema_version=intent.schema_version,
            prompt_version=intent.prompt_version,
            model_name=intent.model_name,
            resolved_entity_ids=list(intent.resolved_entity_ids),
        )
        trace.record(
            events.WORKFLOW_SELECTED,
            conversation_id=conversation_id, turn_id=turn_id,
            workflow_id=trace.workflow_id, selected_by="WorkflowPlanner",
        )
        trace.record(
            events.CHAT_RESPONSE_GENERATED,
            conversation_id=conversation_id, turn_id=turn_id,
            provenance=response.provenance, status=response.status,
            grounding_status=response.grounding_status,
            governance=(response.governance or {}).get("classification"),
        )

    # ==================================================================
    # Introspection
    # ==================================================================

    def history(self, conversation_id: str) -> List[ChatTurn]:
        return self.store.history(conversation_id)
