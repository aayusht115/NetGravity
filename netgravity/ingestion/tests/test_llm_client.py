"""
LLM client tests — provider routing, failure handling, config resolution.

No network calls: the vendor SDK is replaced with a fake, so these run
offline and in CI exactly as they do locally.
"""

from __future__ import annotations

import os

import pytest

from netgravity.ingestion.ai.client import (
    LLMCallError,
    LLMClient,
    _is_unsupported_param,
    _parse_json,
)
from netgravity.ingestion.config import IngestionConfig


# --- fakes ------------------------------------------------------------------

class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish_reason="stop"):
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _Resp:
    def __init__(self, content, finish_reason="stop", usage=None):
        self.choices = [_Choice(content, finish_reason)]
        self.usage = usage


class _FakeCompletions:
    def __init__(self, content='{"ok": true}', finish_reason="stop",
                 fail_with=None, record=None, usage=None):
        self._content = content
        self._finish = finish_reason
        # A single exception is consumed once (existing tests rely on this).
        # A list is consumed one-per-call, oldest first, so a test can prove
        # the fallback loop survives more than one rejected parameter.
        self._fail_queue = list(fail_with) if isinstance(fail_with, list) \
            else ([fail_with] if fail_with is not None else [])
        self.record = record if record is not None else []
        self._usage = usage

    def create(self, **kwargs):
        self.record.append(kwargs)
        if self._fail_queue:
            raise self._fail_queue.pop(0)
        return _Resp(self._content, self._finish, usage=self._usage)


class _FakeOpenAI:
    def __init__(self, completions):
        self.chat = type("chat", (), {"completions": completions})()


def _live_config(**over) -> IngestionConfig:
    cfg = IngestionConfig()
    cfg.llm_api_key = "test-key"          # forces live mode
    cfg.llm_provider = "openai"
    cfg.llm_model = "gpt-4o-mini"
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


# --- config resolution ------------------------------------------------------

def test_default_provider_is_openai():
    assert IngestionConfig().llm_provider == "openai"


def test_model_defaults_follow_the_provider():
    """Switching provider must not leave the other vendor's model name behind."""
    cfg = IngestionConfig()
    cfg.llm_model = None

    cfg.llm_provider = "openai"
    assert cfg.resolved_model.startswith("gpt")

    cfg.llm_provider = "anthropic"
    assert cfg.resolved_model.startswith("claude")


def test_explicit_model_always_wins():
    cfg = IngestionConfig()
    cfg.llm_provider = "openai"
    cfg.llm_model = "some-custom-model"
    assert cfg.resolved_model == "some-custom-model"


def test_no_key_means_stub_mode():
    cfg = IngestionConfig()
    cfg.llm_api_key = None
    assert cfg.stub_mode is True
    assert LLMClient(cfg).stub_mode is True


def test_base_url_is_unset_by_default():
    """Plain OpenAI use (the common case) must not carry a stray base_url."""
    assert IngestionConfig().llm_base_url is None


def test_base_url_env_var_is_picked_up(monkeypatch, isolated_env):
    monkeypatch.setenv("NETGRAVITY_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    assert IngestionConfig().llm_base_url == "https://openrouter.ai/api/v1"


# --- provider routing -------------------------------------------------------

def test_codex_is_accepted_as_an_openai_alias():
    """The team says 'codex'; it speaks the OpenAI API. Don't make them care."""
    client = LLMClient(_live_config(llm_provider="codex"))
    client._sdk = _FakeOpenAI(_FakeCompletions('{"ok": true}'))
    assert client._call_live("give me json", max_tokens=100) == '{"ok": true}'


def test_unknown_provider_is_named_clearly():
    client = LLMClient(_live_config(llm_provider="llama"))
    with pytest.raises(NotImplementedError) as exc:
        client._call_live("p", max_tokens=10)
    assert "llama" in str(exc.value)
    assert "openai" in str(exc.value)


def test_openai_call_requests_json_mode():
    """JSON mode is what stops the model wrapping output in prose."""
    completions = _FakeCompletions('{"ok": true}')
    client = LLMClient(_live_config())
    client._sdk = _FakeOpenAI(completions)
    client._call_live("return json", max_tokens=123)
    sent = completions.record[0]
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["model"] == "gpt-4o-mini"


def test_gemini_via_base_url_gets_reasoning_effort_disabled():
    """
    Gemini's OpenAI-compatible endpoint spends part of max_tokens on hidden
    'thinking' by default, so a small max_tokens (like our own 20-token
    handshake probe) can be fully consumed before any visible answer is
    written - the call succeeds but comes back looking truncated/empty,
    which is easy to mistake for 'never reached the provider'.
    reasoning_effort='none' must be sent whenever the endpoint is Gemini's.
    """
    completions = _FakeCompletions('{"ok": true}')
    client = LLMClient(_live_config(
        llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        llm_model="gemini-2.5-flash",
    ))
    client._sdk = _FakeOpenAI(completions)
    client._call_live("return json", max_tokens=20)
    assert completions.record[0]["reasoning_effort"] == "none"


def test_gemini_detected_by_model_name_even_without_matching_base_url():
    """Belt-and-suspenders: catch it by model name too, in case some other
    base_url ever proxies to a Gemini model."""
    completions = _FakeCompletions('{"ok": true}')
    client = LLMClient(_live_config(llm_model="gemini-2.5-flash"))
    client._sdk = _FakeOpenAI(completions)
    client._call_live("return json", max_tokens=20)
    assert completions.record[0]["reasoning_effort"] == "none"


def test_non_gemini_providers_never_get_reasoning_effort():
    """reasoning_effort is meaningless (and possibly rejected) by a
    non-reasoning model like gpt-4o-mini — must not be sent by default."""
    completions = _FakeCompletions('{"ok": true}')
    client = LLMClient(_live_config())  # default: openai / gpt-4o-mini, no base_url
    client._sdk = _FakeOpenAI(completions)
    client._call_live("return json", max_tokens=20)
    assert "reasoning_effort" not in completions.record[0]


def test_token_usage_is_captured_and_surfaced_in_the_report():
    """
    The provider's own usage figures (not an estimate) end up on
    LLMResponse.tokens AND get folded into .notes, which every adapter
    already prints in the ingestion report — so cost is visible without a
    separate step or a trip to the provider's dashboard.
    """
    completions = _FakeCompletions(
        '{"ok": true}', usage=_Usage(prompt_tokens=1450, completion_tokens=73,
                                     total_tokens=1523),
    )
    client = LLMClient(_live_config())
    client._sdk = _FakeOpenAI(completions)
    response = client.extract_json(task="probe", prompt="p", stub_key="contract")

    assert response.tokens == {
        "prompt_tokens": 1450, "completion_tokens": 73, "total_tokens": 1523,
    }
    assert "1523 tokens" in response.notes
    assert "1450 in" in response.notes
    assert "73 out" in response.notes


def test_no_usage_on_response_means_tokens_is_none_not_a_crash():
    """Some OpenAI-compatible providers may omit usage entirely — must
    degrade gracefully, not KeyError/AttributeError."""
    completions = _FakeCompletions('{"ok": true}', usage=None)
    client = LLMClient(_live_config())
    client._sdk = _FakeOpenAI(completions)
    response = client.extract_json(task="probe", prompt="p", stub_key="contract")

    assert response.tokens is None
    assert "tokens" not in response.notes


def test_falls_back_to_max_tokens_when_model_rejects_the_new_name():
    """
    Newer models renamed max_tokens -> max_completion_tokens. We try the new
    name and switch on the specific complaint, rather than hardcoding a model
    list that goes stale.
    """
    completions = _FakeCompletions(
        '{"ok": true}',
        fail_with=TypeError("Unsupported parameter: 'max_completion_tokens'"),
    )
    client = LLMClient(_live_config())
    client._sdk = _FakeOpenAI(completions)
    client._call_live("return json", max_tokens=77)

    assert "max_completion_tokens" in completions.record[0]
    assert completions.record[1]["max_tokens"] == 77


def test_falls_back_when_provider_rejects_json_mode():
    """
    Not every OpenAI-compatible provider (OpenRouter, Groq, Cerebras, GitHub
    Models, ...) supports response_format identically. A provider that
    rejects it must still get a usable request, not a hard failure.
    """
    completions = _FakeCompletions(
        '{"ok": true}',
        fail_with=TypeError("Unsupported parameter: 'response_format' is not "
                            "supported with this model."),
    )
    client = LLMClient(_live_config())
    client._sdk = _FakeOpenAI(completions)
    client._call_live("return json", max_tokens=77)

    assert completions.record[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in completions.record[1]


def test_falls_back_through_both_rejections_in_one_call():
    """Both quirks can happen on the same alternate provider — the loop must
    survive shedding max_completion_tokens AND response_format, in either
    order, without giving up early."""
    completions = _FakeCompletions(
        '{"ok": true}',
        fail_with=[
            TypeError("Unsupported parameter: 'max_completion_tokens'"),
            TypeError("Unsupported parameter: 'response_format' is not supported"),
        ],
    )
    client = LLMClient(_live_config())
    client._sdk = _FakeOpenAI(completions)
    result = client._call_live("return json", max_tokens=77)

    assert result == '{"ok": true}'
    assert len(completions.record) == 3
    assert "max_completion_tokens" in completions.record[0]
    assert completions.record[1]["max_tokens"] == 77
    assert "response_format" in completions.record[1]
    assert "response_format" not in completions.record[2]


def test_base_url_defaults_to_none_which_means_real_openai(monkeypatch):
    """No NETGRAVITY_LLM_BASE_URL set => the SDK gets base_url=None, which is
    its own signal to use the real api.openai.com. This must not silently
    regress to some other default."""
    captured = {}

    class _CapturingOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = type(
                "chat", (), {"completions": _FakeCompletions('{"ok": true}')}
            )()

    monkeypatch.setattr("openai.OpenAI", _CapturingOpenAI)
    client = LLMClient(_live_config())
    client._call_live("return json", max_tokens=10)

    assert captured["base_url"] is None


def test_base_url_is_forwarded_to_the_sdk_when_configured(monkeypatch):
    """
    This IS the OpenRouter/Groq/Cerebras/GitHub-Models switch: point the same
    OpenAI SDK at a different server by setting one config value. No other
    code path changes for those providers.
    """
    captured = {}

    class _CapturingOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = type(
                "chat", (), {"completions": _FakeCompletions('{"ok": true}')}
            )()

    monkeypatch.setattr("openai.OpenAI", _CapturingOpenAI)
    cfg = _live_config(llm_base_url="https://openrouter.ai/api/v1")
    client = LLMClient(cfg)
    client._call_live("return json", max_tokens=10)

    assert captured["base_url"] == "https://openrouter.ai/api/v1"


def test_an_unrelated_error_is_not_swallowed_by_the_retry():
    completions = _FakeCompletions(fail_with=ValueError("billing quota exceeded"))
    client = LLMClient(_live_config())
    client._sdk = _FakeOpenAI(completions)
    with pytest.raises(ValueError, match="billing"):
        client._call_live("p", max_tokens=10)


def test_truncated_response_names_the_real_cause():
    """A cut-off response is invalid JSON; say why instead of a parse error."""
    client = LLMClient(_live_config())
    client._sdk = _FakeOpenAI(_FakeCompletions('{"ok": tr', finish_reason="length"))
    with pytest.raises(ValueError, match="truncated"):
        client._call_live("p", max_tokens=10)


# --- failure handling -------------------------------------------------------

def test_failure_degrades_to_stub_but_is_marked_as_a_failure():
    client = LLMClient(_live_config())
    client._sdk = _FakeOpenAI(_FakeCompletions(fail_with=RuntimeError("network down")))
    resp = client.extract_json(task="t", prompt="p", stub_key="contract")

    assert resp.stubbed is True
    assert resp.failed is True                    # not ordinary stub mode
    assert "LLM CALL FAILED" in resp.notes
    assert "NOT a real extraction" in resp.notes


def test_strict_mode_raises_instead_of_substituting_stub_data():
    client = LLMClient(_live_config(llm_strict=True))
    client._sdk = _FakeOpenAI(_FakeCompletions(fail_with=RuntimeError("network down")))
    with pytest.raises(LLMCallError, match="refusing to substitute stub data"):
        client.extract_json(task="t", prompt="p", stub_key="contract")


def test_ordinary_stub_mode_is_not_flagged_as_a_failure():
    cfg = IngestionConfig()
    cfg.llm_api_key = None
    resp = LLMClient(cfg).extract_json(task="t", prompt="p", stub_key="contract")
    assert resp.stubbed is True
    assert resp.failed is False


# --- helpers ----------------------------------------------------------------

def test_unsupported_param_detection_is_specific():
    assert _is_unsupported_param(
        Exception("Unsupported parameter: 'max_completion_tokens'"),
        "max_completion_tokens",
    )
    assert not _is_unsupported_param(Exception("rate limit exceeded"),
                                     "max_completion_tokens")


def test_json_parser_tolerates_fences_and_prose():
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('Sure!\n{"a": 1}\nHope that helps.') == {"a": 1}


# --- the provider switch ----------------------------------------------------

def test_default_is_openai_when_flag_is_absent(monkeypatch):
    monkeypatch.delenv("NETGRAVITY_USE_CLAUDE", raising=False)
    assert IngestionConfig().llm_provider == "openai"


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_flag_switches_to_claude(monkeypatch, value):
    monkeypatch.setenv("NETGRAVITY_USE_CLAUDE", value)
    assert IngestionConfig().llm_provider == "anthropic"


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_falsey_flag_stays_on_openai(monkeypatch, value):
    monkeypatch.setenv("NETGRAVITY_USE_CLAUDE", value)
    assert IngestionConfig().llm_provider == "openai"


def test_the_flag_is_the_only_provider_switch(monkeypatch):
    """
    One switch, no precedence rules. A stray NETGRAVITY_LLM_PROVIDER left in
    an old .env must not silently change the vendor.
    """
    monkeypatch.setenv("NETGRAVITY_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("NETGRAVITY_USE_CLAUDE", raising=False)
    assert IngestionConfig().llm_provider == "openai"


def test_azure_openai_is_not_silently_routed_to_public_openai(monkeypatch):
    """
    Azure needs a different client class. Accepting it here would quietly call
    api.openai.com instead of the Azure deployment — worse than not supporting
    it, because it would look like it worked.
    """
    client = LLMClient(_live_config(llm_provider="azure_openai"))
    with pytest.raises(NotImplementedError, match="azure_openai"):
        client._call_live("p", max_tokens=10)


# --- key resolution ---------------------------------------------------------

def test_flag_picks_up_the_matching_key(monkeypatch):
    """
    The point of per-provider keys: with both set, flipping the switch is the
    ONLY edit needed. An OpenAI key is not valid at Anthropic.
    """
    monkeypatch.delenv("NETGRAVITY_LLM_API_KEY", raising=False)
    monkeypatch.setenv("NETGRAVITY_OPENAI_API_KEY", "sk-openai-one")
    monkeypatch.setenv("NETGRAVITY_ANTHROPIC_API_KEY", "sk-ant-two")

    monkeypatch.setenv("NETGRAVITY_USE_CLAUDE", "false")
    assert IngestionConfig().llm_api_key == "sk-openai-one"

    monkeypatch.setenv("NETGRAVITY_USE_CLAUDE", "true")
    assert IngestionConfig().llm_api_key == "sk-ant-two"


def test_generic_key_still_works_on_its_own(monkeypatch):
    """The original variable name keeps working — no forced migration."""
    for var in ("NETGRAVITY_OPENAI_API_KEY", "NETGRAVITY_ANTHROPIC_API_KEY",
                "NETGRAVITY_USE_CLAUDE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NETGRAVITY_LLM_API_KEY", "sk-generic")
    cfg = IngestionConfig()
    assert cfg.llm_api_key == "sk-generic"
    assert cfg.stub_mode is False


def test_provider_specific_key_beats_the_generic_one(monkeypatch):
    monkeypatch.delenv("NETGRAVITY_USE_CLAUDE", raising=False)
    monkeypatch.setenv("NETGRAVITY_LLM_API_KEY", "sk-generic")
    monkeypatch.setenv("NETGRAVITY_OPENAI_API_KEY", "sk-specific")
    assert IngestionConfig().llm_api_key == "sk-specific"


def test_no_key_anywhere_means_stub_mode(monkeypatch):
    for var in ("NETGRAVITY_LLM_API_KEY", "NETGRAVITY_OPENAI_API_KEY",
                "NETGRAVITY_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert IngestionConfig().stub_mode is True


# --- the mismatch guard -----------------------------------------------------

def test_warns_when_switched_to_claude_with_an_openai_key(monkeypatch):
    """Otherwise this surfaces only as an opaque 401 mid-run."""
    monkeypatch.delenv("NETGRAVITY_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("NETGRAVITY_USE_CLAUDE", "true")
    monkeypatch.setenv("NETGRAVITY_LLM_API_KEY", "sk-proj-abc")
    warning = IngestionConfig().key_warning
    assert warning and "looks like an OpenAI key" in warning


def test_warns_when_on_openai_with_an_anthropic_key(monkeypatch):
    monkeypatch.delenv("NETGRAVITY_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NETGRAVITY_USE_CLAUDE", raising=False)
    monkeypatch.setenv("NETGRAVITY_LLM_API_KEY", "sk-ant-abc")
    warning = IngestionConfig().key_warning
    assert warning and "looks like an Anthropic key" in warning


def test_matching_key_produces_no_warning(monkeypatch):
    monkeypatch.delenv("NETGRAVITY_USE_CLAUDE", raising=False)
    monkeypatch.setenv("NETGRAVITY_OPENAI_API_KEY", "sk-proj-abc")
    assert IngestionConfig().key_warning is None


def test_no_key_produces_no_warning(monkeypatch):
    for var in ("NETGRAVITY_LLM_API_KEY", "NETGRAVITY_OPENAI_API_KEY",
                "NETGRAVITY_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert IngestionConfig().key_warning is None


# --- .env loading -----------------------------------------------------------

@pytest.fixture
def isolated_env():
    """
    Snapshot and restore os.environ around a test.

    _load_dotenv_once() writes into os.environ directly, and monkeypatch does
    not know about keys it did not set itself. Without this, a leaked test key
    puts LATER tests into live mode and they try to reach the network.
    """
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_dotenv_is_actually_read(tmp_path, monkeypatch, isolated_env):
    """
    A key pasted into .env must reach the config. Before this was wired up,
    config only read os.environ — so .env was a file nobody read, and the
    pipeline stayed silently in stub mode while looking configured.
    """
    from netgravity.ingestion import config as config_mod

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment line\n"
        "NETGRAVITY_OPENAI_API_KEY=sk-from-file\n"
        "\n"
        'NETGRAVITY_LLM_MODEL="gpt-quoted"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    for var in ("NETGRAVITY_OPENAI_API_KEY", "NETGRAVITY_LLM_MODEL",
                "NETGRAVITY_LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    assert config_mod._load_dotenv_once() == str(env_file)
    assert os.environ["NETGRAVITY_OPENAI_API_KEY"] == "sk-from-file"
    assert os.environ["NETGRAVITY_LLM_MODEL"] == "gpt-quoted"   # quotes stripped


def test_real_environment_wins_over_the_dotenv_file(tmp_path, monkeypatch,
                                                   isolated_env):
    """
    CI and Azure inject real env vars. A stale committed .env must never
    quietly override them.
    """
    from netgravity.ingestion import config as config_mod

    (tmp_path / ".env").write_text("NETGRAVITY_LLM_MODEL=from-file\n",
                                   encoding="utf-8")
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("NETGRAVITY_LLM_MODEL", "from-shell")

    config_mod._load_dotenv_once()
    assert os.environ["NETGRAVITY_LLM_MODEL"] == "from-shell"


def test_missing_dotenv_is_not_an_error(tmp_path, monkeypatch, isolated_env):
    from netgravity.ingestion import config as config_mod
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    assert config_mod._load_dotenv_once() is None
