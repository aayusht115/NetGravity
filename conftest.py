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

import os
import tempfile
import uuid

import pytest

# ---------------------------------------------------------------------------
# The test suite must never touch the real database.
#
# Set at conftest IMPORT time — before any test module, and therefore before
# `app.backend.app` is imported and `persistence.database` is constructed. A
# fixture would run too late: the connection is opened when the module is
# imported, which happens during collection.
#
# Accounts, projects and uploads are now persisted, so without this the suite
# would write into `data/netgravity.db` and, worse, would start FAILING on the
# second run — `test_signup_success` registers a fixed address, and signup
# correctly refuses an email that already exists. A test that passes only on a
# clean machine is not a passing test.
#
# One file per pytest process, in the OS temp directory, removed by the OS.
_TEST_DB = os.path.join(
    tempfile.gettempdir(), f"netgravity-test-{uuid.uuid4().hex[:12]}.db"
)
os.environ["NETGRAVITY_DB_PATH"] = _TEST_DB

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
    # OpenAI Agents SDK reasoning. Removing the runtime selector as well as the
    # key makes a live call structurally impossible in the default suite.
    "OPENAI_API_KEY",
    "NETGRAVITY_REASONING_RUNTIME",
    "NETGRAVITY_REASONING_MODEL",
)


@pytest.fixture(autouse=True)
def _fresh_rate_limit_window():
    """
    Give every test its own rate-limit budget.

    The limiter identifies a client by authenticated user or peer address, and
    in a test client every request comes from the same non-address — so the
    whole suite lands in ONE bucket and the twentieth login anywhere in it is
    refused. Resetting between tests keeps the limiter switched on and
    exercised (a dedicated test asserts it refuses past the threshold) rather
    than disabling it and shipping it untested.
    """
    from app.backend.services.ratelimit import limiter
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _no_ambient_llm_credentials(monkeypatch):
    # Force the config module to consume `.env` before credentials are
    # removed. Some integration paths import it lazily inside the test; if
    # that first import happened after the deletions below, `_load_dotenv_once`
    # would repopulate the environment and silently turn an offline test into
    # a live, budget-consuming model call. Importing first makes isolation
    # independent of test collection and execution order.
    from netgravity.ingestion import config as _ingestion_config  # noqa: F401

    for var in _CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
