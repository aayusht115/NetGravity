"""
NetGravity — Action Agent Recipients
=======================================
Two separate lists, per the spec:

  NotificationRecipientStore   the standing list for triggers 3/4
                                (recommendation / investigate emails) —
                                "who generally wants to see this kind of
                                thing".
  SourceContactStore            per-source contact for triggers 1/2/5
                                (missing-data emails + inbound-reply sender
                                verification) — "who owns this specific
                                data source".

Both are plain JSON blobs behind StorageBackend, matching every other piece
of durable-but-not-network-optimizer-critical state in this codebase
(IngestionSessionStore, FieldMemory, FieldCatalog).

Seed values (NETGRAVITY_DEFAULT_RECIPIENT_EMAIL /
NETGRAVITY_DEFAULT_TEST_RECIPIENT_EMAIL) populate the recipients store ONLY
if it is empty, and only ever as a starting point — every address is fully
editable afterward through this same store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from netgravity.action_agent.config import ActionAgentConfig, load_config
from netgravity.ingestion.storage.base import StorageBackend

ZONE = "standardized"
RECIPIENTS_KEY = "action_agent/notification_recipients.json"
#: One book per scope, alongside the unscoped one above.
#:
#: The store was a single deployment-wide file. Every project on the
#: installation shared one address list, so an address added while working
#: on one client's network was offered — by name — on the next client's.
#: Measured: a recipient added on one project appeared, pre-ticked, on a
#: newly created project belonging to a different account.
#:
#: The unscoped file is kept and is still the default, because the pipeline's
#: own triggers (a governance card, an ingestion session) have no project in
#: hand at the point they fire. Anything reached from a project screen passes
#: one, and gets its own book.
RECIPIENTS_SCOPED_PREFIX = "action_agent/notification_recipients"
SOURCE_CONTACTS_PREFIX = "action_agent/source_contacts"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_source_id(source_id: str) -> str:
    safe = "".join(ch for ch in source_id if ch.isalnum() or ch in "_-")
    if not safe or safe != source_id:
        raise ValueError(f"invalid source id: {source_id!r}")
    return safe


@dataclass
class Recipient:
    label: str
    email: str
    created_at: str = field(default_factory=_now)

    def as_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "email": self.email, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Recipient":
        return cls(label=str(raw.get("label") or ""), email=str(raw.get("email") or ""),
                   created_at=str(raw.get("created_at") or _now()))


class NotificationRecipientStore:
    """
    The standing list for triggers 3/4 (recommendation / investigate).

    `scope` is a project id, or None for the deployment-wide book the
    pipeline's own triggers use — see RECIPIENTS_SCOPED_PREFIX for why both
    exist. A scoped book is seeded independently of the unscoped one, so a
    new project starts with the configured defaults and nothing else.
    """

    def __init__(self, storage: StorageBackend, config: Optional[ActionAgentConfig] = None,
                 scope: Optional[str] = None):
        self.storage = storage
        self.config = config or load_config()
        self.scope = _safe_source_id(scope) if scope else None

    @property
    def key(self) -> str:
        if self.scope:
            return f"{RECIPIENTS_SCOPED_PREFIX}/{self.scope}.json"
        return RECIPIENTS_KEY

    def list(self) -> List[Recipient]:
        self._seed_if_empty()
        if not self.storage.exists(ZONE, self.key):
            return []
        raw = json.loads(self.storage.get_text(ZONE, self.key))
        return [Recipient.from_dict(r) for r in raw]

    def _save(self, recipients: List[Recipient]) -> None:
        self.storage.save_text(
            ZONE, self.key,
            json.dumps([r.as_dict() for r in recipients], indent=2, default=str))

    def _seed_if_empty(self) -> None:
        if self.storage.exists(ZONE, self.key):
            return
        seeded: List[Recipient] = []
        if self.config.default_recipient_email:
            seeded.append(Recipient(label="Me", email=self.config.default_recipient_email))
        if self.config.default_test_recipient_email:
            seeded.append(Recipient(label="Client (test)",
                                    email=self.config.default_test_recipient_email))
        self._save(seeded)

    def add(self, email: str, label: str = "") -> Recipient:
        """
        Add (or return, if already present) a recipient.

        This is also the "learning" mechanic the spec describes: an address
        typed beyond the standing list at send time is meant to be appended
        here for next time, not just used once.
        """
        recipients = self.list()
        existing = next((r for r in recipients if r.email.lower() == email.lower()), None)
        if existing:
            return existing
        new_recipient = Recipient(label=label or email, email=email)
        recipients.append(new_recipient)
        self._save(recipients)
        return new_recipient

    def remove(self, email: str) -> None:
        recipients = [r for r in self.list() if r.email.lower() != email.lower()]
        self._save(recipients)

    def emails(self) -> List[str]:
        return [r.email for r in self.list()]


class SourceContactStore:
    """Per-source contact for triggers 1/2/5."""

    def __init__(self, storage: StorageBackend, config: Optional[ActionAgentConfig] = None):
        self.storage = storage
        self.config = config or load_config()

    def _key(self, source_id: str) -> str:
        return f"{SOURCE_CONTACTS_PREFIX}/{_safe_source_id(source_id)}.json"

    def get(self, source_id: str) -> Optional[Recipient]:
        key = self._key(source_id)
        if not self.storage.exists(ZONE, key):
            if self.config.default_test_recipient_email:
                # Default placeholder contact for any mock/test source until
                # a real distributor contact is captured — per spec §6.
                return Recipient(label="Client (test)",
                                 email=self.config.default_test_recipient_email)
            return None
        return Recipient.from_dict(json.loads(self.storage.get_text(ZONE, key)))

    def set(self, source_id: str, email: str, contact_name: str = "") -> Recipient:
        contact = Recipient(label=contact_name or email, email=email)
        self.storage.save_text(ZONE, self._key(source_id),
                               json.dumps(contact.as_dict(), indent=2, default=str))
        return contact

    def verify_sender(self, source_id: str, from_address: str) -> bool:
        """
        Basic guard against a spoofed reply claiming to be the data owner.

        Not bulletproof — email sender addresses can be spoofed — but it
        stops the obvious case of anyone who discovers the inbound address
        submitting unverified data. A mismatch (or no registered contact at
        all) means "hold for manual review", never "accept anyway".
        """
        contact = self.get(source_id)
        if contact is None or not contact.email:
            return False
        return contact.email.strip().lower() == (from_address or "").strip().lower()
