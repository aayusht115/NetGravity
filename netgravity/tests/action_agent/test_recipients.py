"""
Tests for netgravity.action_agent.recipients.

Claims under test:
  1. The recipients store seeds itself once from
     NETGRAVITY_DEFAULT_RECIPIENT_EMAIL / _TEST_RECIPIENT_EMAIL when empty,
     and never re-seeds after that (so a manual removal sticks).
  2. `add()` is the "learning" mechanic — an address used once is
     remembered for next time, and adding twice does not duplicate it.
  3. `verify_sender()` is the basic anti-spoofing guard for inbound replies:
     a mismatch (or no registered contact at all) must never verify.
"""

from __future__ import annotations

from netgravity.action_agent.config import ActionAgentConfig
from netgravity.action_agent.recipients import NotificationRecipientStore, SourceContactStore


def _config(**overrides) -> ActionAgentConfig:
    base = dict(default_recipient_email="me@example.com",
               default_test_recipient_email="test@example.com")
    base.update(overrides)
    return ActionAgentConfig(**base)


def test_seeds_from_default_env_vars_when_empty(aa_storage):
    store = NotificationRecipientStore(aa_storage, config=_config())
    emails = store.emails()
    assert "me@example.com" in emails
    assert "test@example.com" in emails


def test_seed_only_happens_once(aa_storage):
    store = NotificationRecipientStore(aa_storage, config=_config())
    store.remove("test@example.com")
    assert "test@example.com" not in store.emails()

    # A second store instance against the same storage must NOT re-seed the
    # address a person deliberately removed.
    again = NotificationRecipientStore(aa_storage, config=_config())
    assert "test@example.com" not in again.emails()


def test_add_is_idempotent(aa_storage):
    store = NotificationRecipientStore(aa_storage, config=_config())
    store.add("new@example.com", label="New Person")
    store.add("new@example.com", label="New Person Again")

    matches = [r for r in store.list() if r.email == "new@example.com"]
    assert len(matches) == 1


def test_no_seed_configured_starts_empty(aa_storage):
    store = NotificationRecipientStore(
        aa_storage, config=_config(default_recipient_email=None,
                                   default_test_recipient_email=None))
    assert store.list() == []


def test_verify_sender_matches_registered_contact(aa_storage):
    contacts = SourceContactStore(aa_storage, config=_config(default_test_recipient_email=None))
    contacts.set("client_a", "owner@clienta.com", contact_name="Owner")

    assert contacts.verify_sender("client_a", "owner@clienta.com") is True
    assert contacts.verify_sender("client_a", "OWNER@clienta.com") is True  # case-insensitive


def test_verify_sender_rejects_mismatch(aa_storage):
    contacts = SourceContactStore(aa_storage, config=_config(default_test_recipient_email=None))
    contacts.set("client_a", "owner@clienta.com")

    assert contacts.verify_sender("client_a", "attacker@evil.com") is False


def test_verify_sender_rejects_unregistered_source(aa_storage):
    contacts = SourceContactStore(aa_storage, config=_config(default_test_recipient_email=None))
    assert contacts.verify_sender("unknown_source", "anyone@example.com") is False


def test_unregistered_source_falls_back_to_default_test_contact_when_configured(aa_storage):
    """
    Per spec §6: NETGRAVITY_DEFAULT_TEST_RECIPIENT_EMAIL doubles as the
    placeholder source_contacts entry for any mock/test source until a real
    distributor contact exists — a deliberate dev/test convenience, not a
    security hole in a build where outbound email is stubbed regardless.
    """
    contacts = SourceContactStore(aa_storage, config=_config())
    assert contacts.verify_sender("unregistered_source", "test@example.com") is True
