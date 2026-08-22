"""
Repo-wide pytest fixtures.

`IngestionConfig` reads credentials straight out of the environment (and
`.env`), so once real provider credentials are configured for live
verification work, every test that builds a plain `IngestionConfig()`
silently stops being a stub-mode test — it makes a real, possibly slow or
budget-consuming, live call instead. That breaks the suite's own contract
("574 passing, ~12s, zero live API calls, runs with no credentials at
all") without any test file changing.

This autouse fixture strips every credential / provider-switch variable
before each test, so the suite is hermetic regardless of what is
configured in `.env` on the machine running it. A test that wants live
behaviour sets its own key or provider explicitly (via monkeypatch or by
setting the attribute directly on a constructed config), which still
works fine since that happens after this fixture runs.
"""
from __future__ import annotations

import pytest

_CREDENTIAL_ENV_VARS = (
    "NETGRAVITY_USE_CLAUDE",
    "NETGRAVITY_USE_GATEWAY",
    "NETGRAVITY_LLM_API_KEY",
    "NETGRAVITY_OPENAI_API_KEY",
    "NETGRAVITY_ANTHROPIC_API_KEY",
    "NETGRAVITY_GATEWAY_URL",
    "NETGRAVITY_GATEWAY_TOKEN",
    "NETGRAVITY_LLM_BASE_URL",
    "NETGRAVITY_LLM_MODEL",
)


@pytest.fixture(autouse=True)
def _no_ambient_llm_credentials(monkeypatch):
    for var in _CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
