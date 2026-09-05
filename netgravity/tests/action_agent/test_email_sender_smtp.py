"""
Tests for the real SMTP path in netgravity.action_agent.email_sender._send_live.

Claims under test:
  1. STARTTLS is used by default (what Gmail/most real providers require),
     and can be turned off for a local relay with no TLS.
  2. A configured username/password logs in before sending.
  3. The From: header falls back smtp_from_address -> smtp_username ->
     a placeholder, in that order.
  4. host/port/recipients are passed through correctly.

Uses a fake smtplib.SMTP (matching the repo's _Fake... convention) rather
than mocking — nothing here ever opens a real socket.
"""

from __future__ import annotations

import smtplib

import pytest

from netgravity.action_agent.config import ActionAgentConfig
from netgravity.action_agent.email_sender import EmailSender


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_args = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent = {"msg": msg, "from_addr": from_addr, "to_addrs": to_addrs}


@pytest.fixture(autouse=True)
def _patch_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    yield
    _FakeSMTP.instances = []


def test_starttls_used_by_default():
    cfg = ActionAgentConfig(smtp_host="smtp.gmail.com", smtp_username="me@gmail.com",
                            smtp_password="app-password")
    EmailSender(cfg).send(to=["owner@example.com"], subject="s", body="b")

    smtp = _FakeSMTP.instances[0]
    assert smtp.host == "smtp.gmail.com"
    assert smtp.port == 587
    assert smtp.starttls_called is True
    assert smtp.login_args == ("me@gmail.com", "app-password")


def test_starttls_skipped_when_disabled():
    cfg = ActionAgentConfig(smtp_host="localhost", smtp_use_tls=False)
    EmailSender(cfg).send(to=["owner@example.com"], subject="s", body="b")

    smtp = _FakeSMTP.instances[0]
    assert smtp.starttls_called is False


def test_login_skipped_when_no_username():
    cfg = ActionAgentConfig(smtp_host="localhost", smtp_username=None)
    EmailSender(cfg).send(to=["owner@example.com"], subject="s", body="b")

    smtp = _FakeSMTP.instances[0]
    assert smtp.login_args is None


def test_from_address_prefers_explicit_from_over_username():
    cfg = ActionAgentConfig(smtp_host="smtp.gmail.com", smtp_username="login@gmail.com",
                            smtp_from_address="noreply@netgravity.example")
    EmailSender(cfg).send(to=["owner@example.com"], subject="s", body="b")

    smtp = _FakeSMTP.instances[0]
    assert smtp.sent["from_addr"] == "noreply@netgravity.example"
    assert smtp.sent["msg"]["From"] == "noreply@netgravity.example"


def test_from_address_falls_back_to_username_then_placeholder():
    cfg = ActionAgentConfig(smtp_host="smtp.gmail.com", smtp_username="login@gmail.com")
    EmailSender(cfg).send(to=["owner@example.com"], subject="s", body="b")
    assert _FakeSMTP.instances[0].sent["from_addr"] == "login@gmail.com"

    cfg2 = ActionAgentConfig(smtp_host="localhost")
    EmailSender(cfg2).send(to=["owner@example.com"], subject="s", body="b")
    assert _FakeSMTP.instances[1].sent["from_addr"] == "netgravity@localhost"


def test_recipients_and_reply_to_pass_through():
    cfg = ActionAgentConfig(smtp_host="smtp.gmail.com")
    EmailSender(cfg).send(to=["a@example.com", "b@example.com"], subject="s", body="b",
                         reply_to="ingest-ing_1@mail.example.com")

    smtp = _FakeSMTP.instances[0]
    assert smtp.sent["to_addrs"] == ["a@example.com", "b@example.com"]
    assert smtp.sent["msg"]["To"] == "a@example.com, b@example.com"
    assert smtp.sent["msg"]["Reply-To"] == "ingest-ing_1@mail.example.com"
