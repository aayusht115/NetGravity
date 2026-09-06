"""
Suite-wide safety: no test reaches a live model, ever.

WHY THIS EXISTS. `TEXT_API_TOKEN` is the reasoning gateway's credential, and
a token present anywhere in the process environment is enough for the
orchestrator to make a real, billable HTTP request. That is one leaked
variable away at all times:

  * a developer's shell, or a `.env` the application loads at import;
  * `load_dotenv` inside a test, which writes straight into `os.environ`
    behind monkeypatch's back — exactly how "from-the-file" once escaped
    `test_operational_hardening.py` and made the suite take a 401 from the
    live gateway.

So the token is cleared for every test, before every test. A test that wants
a configured gateway sets it with `monkeypatch.setenv` AND fakes the
transport; nothing is left to ambient state.

This does not weaken any test. The LLM tier is designed to degrade to
rule-based intent parsing and template reasoning when the token is absent,
and that is the state the suite has always actually run in.
"""

from __future__ import annotations

import pytest

#: Every credential that could make a test call out. The reasoning gateway's
#: token, and the ingestion AI client's keys — a different pipeline, the same
#: hazard.
_LIVE_CREDENTIALS = (
    "TEXT_API_TOKEN",
    "NETGRAVITY_LLM_API_KEY",
    "NETGRAVITY_OPENAI_API_KEY",
    "NETGRAVITY_ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _no_live_model_calls(monkeypatch):
    """
    Clear every model credential for the duration of each test.

    Autouse and unconditional. A test that needs one sets it itself, which
    makes the intent visible at the point of use rather than inherited from
    whatever ran before.
    """
    for name in _LIVE_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
