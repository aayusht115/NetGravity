"""Shared fixtures for the action_agent test suite."""

from __future__ import annotations

import pytest

from netgravity.action_agent.config import ActionAgentConfig
from netgravity.ingestion.storage.local import LocalStorage

#: Every env var ActionAgentConfig reads. A developer's real .env may have
#: live SMTP credentials configured (see scripts/verify_9_action_agent.py's
#: live-mode run) — without clearing these, ActionAgentConfig() picks them
#: up inside a test the same way it would in production, silently turning a
#: "no credentials configured" test into a real (or real-shaped) SMTP
#: attempt. Every test in this package must run in stub mode regardless of
#: what's sitting in the developer's local .env.
_ACTION_AGENT_ENV_VARS = (
    "NETGRAVITY_SMTP_HOST", "NETGRAVITY_SMTP_PORT", "NETGRAVITY_SMTP_USERNAME",
    "NETGRAVITY_SMTP_PASSWORD", "NETGRAVITY_SMTP_USE_TLS", "NETGRAVITY_SMTP_FROM_ADDRESS",
    "NETGRAVITY_EMAIL_API_KEY", "NETGRAVITY_EMAIL_STRICT",
    "NETGRAVITY_DEFAULT_RECIPIENT_EMAIL", "NETGRAVITY_DEFAULT_TEST_RECIPIENT_EMAIL",
    "NETGRAVITY_INBOUND_EMAIL_DOMAIN", "NETGRAVITY_APP_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_action_agent_env(monkeypatch):
    for name in _ACTION_AGENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def aa_config() -> ActionAgentConfig:
    """No SMTP credential configured => stub mode, exactly like production
    defaults until Aayush explicitly signs off on a real one."""
    return ActionAgentConfig(smtp_host=None, email_api_key=None, email_strict=False)


@pytest.fixture
def aa_storage(tmp_path) -> LocalStorage:
    for zone in ("raw", "standardized", "curated"):
        (tmp_path / zone).mkdir(parents=True, exist_ok=True)
    return LocalStorage(tmp_path)
