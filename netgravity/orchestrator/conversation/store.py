"""
Orchestrator — Conversation state.

Holds turn history so follow-ups ("why?", "what about Mumbai?") can be
interpreted. Deliberately small: a conversation is a list of turns and the
entities the last one was about.

WHAT THIS DOES NOT ACCUMULATE
─────────────────────────────
Scenario overrides. Each what-if is built fresh from the OBSERVED baseline and
the current turn's intent. If a conversation carried overrides forward, turn
three would silently analyse "Delhi closed AND Mumbai closed AND capacity cut"
while the user believed they had asked one question. Every scenario in a
conversation therefore branches from the same observed snapshot, never from the
previous scenario.

Baseline state is never touched here at all — this store holds text and ids.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from netgravity.orchestrator.schemas.conversation import ChatTurn
from netgravity.orchestrator.schemas.requests import Intent

logger = logging.getLogger(__name__)

#: Turns retained per conversation. Bounded so a long session cannot grow
#: without limit, matching the audit logger's ring-buffer discipline.
MAX_TURNS = 50


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Conversation:
    """One user session."""
    conversation_id: str
    created_at: str = field(default_factory=_utc_now)
    turns: List[ChatTurn] = field(default_factory=list)
    #: Snapshot this conversation is reading. Pinned on the first turn so a
    #: mid-conversation data refresh is detected rather than silently applied.
    network_snapshot_id: Optional[str] = None

    @property
    def last_turn(self) -> Optional[ChatTurn]:
        return self.turns[-1] if self.turns else None

    @property
    def last_entity_ids(self) -> List[str]:
        """Entities the most recent SUBSTANTIVE turn was about."""
        for turn in reversed(self.turns):
            if turn.resolved_entity_ids:
                return list(turn.resolved_entity_ids)
        return []

    @property
    def last_intent(self) -> Optional[Intent]:
        for turn in reversed(self.turns):
            if turn.intent is not None and turn.intent != Intent.UNKNOWN:
                return turn.intent
        return None

    @property
    def last_scenario_label(self) -> Optional[str]:
        """
        Label of the most recent hypothetical, for context only.

        A label, never a spec. There is no field on `ChatTurn` capable of
        holding a `ScenarioIntentSpec`, so "reduce Delhi by 2,000" can be
        mentioned to a model as something that was previously asked and can
        never be silently re-applied.
        """
        for turn in reversed(self.turns):
            if turn.scenario_label:
                return turn.scenario_label
        return None

    def context(self, available_entity_ids: Optional[List[str]] = None):
        """
        The bounded slice of this conversation a model may see.

        Built here rather than in `ChatService` so there is exactly one
        definition of what leaves the store, and it is a schema rather than a
        transcript.
        """
        from netgravity.orchestrator.schemas.conversation import ConversationContext

        previous_query = None
        for turn in reversed(self.turns):
            if turn.user_input:
                previous_query = turn.user_input
                break

        return ConversationContext(
            current_entity_ids=self.last_entity_ids,
            previous_intent=self.last_intent,
            previous_query=previous_query,
            previous_scenario_label=self.last_scenario_label,
            available_entity_ids=list(available_entity_ids or []),
        )

    @property
    def pending_clarification(self) -> bool:
        last = self.last_turn
        return bool(last and last.clarity.value in
                    ("AMBIGUOUS", "INSUFFICIENT_INFORMATION"))


class ConversationStore:
    """In-memory conversation registry behind a narrow, replaceable interface."""

    def __init__(self, max_conversations: int = 500) -> None:
        self._conversations: Dict[str, Conversation] = {}
        self._order: List[str] = []
        self._max = max_conversations
        self._lock = threading.RLock()

    def start(self, conversation_id: Optional[str] = None) -> Conversation:
        with self._lock:
            cid = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
            existing = self._conversations.get(cid)
            if existing is not None:
                return existing

            conversation = Conversation(conversation_id=cid)
            self._conversations[cid] = conversation
            self._order.append(cid)
            while len(self._order) > self._max:
                self._conversations.pop(self._order.pop(0), None)
            logger.info("conversation.started conversation_id=%s", cid)
            return conversation

    def get(self, conversation_id: str) -> Optional[Conversation]:
        return self._conversations.get(conversation_id)

    def append(self, conversation_id: str, turn: ChatTurn) -> None:
        with self._lock:
            conversation = self.start(conversation_id)
            turn.at = turn.at or _utc_now()
            conversation.turns.append(turn)
            while len(conversation.turns) > MAX_TURNS:
                conversation.turns.pop(0)

    def list_ids(self) -> List[str]:
        return list(self._order)

    def history(self, conversation_id: str) -> List[ChatTurn]:
        conversation = self.get(conversation_id)
        return list(conversation.turns) if conversation else []
