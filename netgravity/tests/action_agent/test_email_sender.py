"""
Tests for netgravity.action_agent.email_sender.

Mirrors the claims netgravity/tests/ingestion/test_llm_client.py makes about
ai/client.py, since email_sender.py is deliberately built to the same
contract: no credential configured => stub mode, no network call; a failed
live send degrades to a labelled stub unless NETGRAVITY_EMAIL_STRICT is set,
in which case it raises.
"""

from __future__ import annotations

import pytest

from netgravity.action_agent.config import ActionAgentConfig
from netgravity.action_agent.email_sender import (
    EMAIL_FAILURE_MARKER,
    EmailSendError,
    EmailSender,
)


def test_no_credential_configured_is_stub_mode(aa_config):
    sender = EmailSender(aa_config)
    assert sender.stub_mode is True

    result = sender.send(to=["owner@example.com"], subject="hi", body="body")
    assert result.sent is True
    assert result.stubbed is True
    assert result.failed is False
    assert result.recipients == ["owner@example.com"]


def test_stub_mode_never_calls_smtp(aa_config, monkeypatch):
    called = []
    monkeypatch.setattr(
        EmailSender, "_send_live",
        lambda self, **kwargs: called.append(kwargs) or (_ for _ in ()).throw(
            AssertionError("stub mode must never call _send_live")),
    )
    EmailSender(aa_config).send(to=["a@b.com"], subject="s", body="b")
    assert called == []


def test_live_send_failure_degrades_to_labelled_stub_by_default(monkeypatch):
    cfg = ActionAgentConfig(smtp_host="smtp.example.com", email_strict=False)
    sender = EmailSender(cfg)
    monkeypatch.setattr(
        EmailSender, "_send_live",
        lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("connection refused")),
    )

    result = sender.send(to=["owner@example.com"], subject="hi", body="body")

    assert result.sent is False
    assert result.stubbed is True
    assert result.failed is True
    assert EMAIL_FAILURE_MARKER in result.notes


def test_live_send_failure_raises_when_strict(monkeypatch):
    cfg = ActionAgentConfig(smtp_host="smtp.example.com", email_strict=True)
    sender = EmailSender(cfg)
    monkeypatch.setattr(
        EmailSender, "_send_live",
        lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("connection refused")),
    )

    with pytest.raises(EmailSendError):
        sender.send(to=["owner@example.com"], subject="hi", body="body")


def test_live_send_success_is_not_stubbed(monkeypatch):
    cfg = ActionAgentConfig(smtp_host="smtp.example.com", email_strict=False)
    sender = EmailSender(cfg)
    monkeypatch.setattr(EmailSender, "_send_live", lambda self, **kwargs: None)

    result = sender.send(to=["owner@example.com"], subject="hi", body="body")

    assert result.sent is True
    assert result.stubbed is False
    assert result.failed is False
