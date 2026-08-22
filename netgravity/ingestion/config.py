"""
NetGravity — Ingestion Configuration
=====================================
Single source of truth for every path, mode and credential the ingestion
pipeline needs. Nothing in the ingestion package may construct a filesystem
path or read an environment variable directly — it all comes from here.

WHY THIS MATTERS FOR AZURE
--------------------------
When this pipeline moves to Azure, no ingestion code changes. Only the
environment variables below change:

    NETGRAVITY_STORAGE_BACKEND   local  ->  azure_blob
    NETGRAVITY_DATA_ROOT         ./data ->  (unused; container names apply)
    NETGRAVITY_LLM_API_KEY       .env   ->  Azure Key Vault secret reference

ENVIRONMENT VARIABLES
---------------------
    NETGRAVITY_DATA_ROOT          Root folder for data zones (default: <repo>/data)
    NETGRAVITY_STORAGE_BACKEND    "local" | "azure_blob"   (default: local)
    NETGRAVITY_AZURE_CONN_STR     Azure Blob connection string (azure_blob only)
    NETGRAVITY_USE_CLAUDE         THE switch — the only one. Unset/false =>
                                  OpenAI (default). "true" => Anthropic Claude.
    NETGRAVITY_LLM_API_KEY        Generic key, used for whichever provider is
                                  active. If ABSENT, the AI client runs in
                                  STUB MODE and returns canned responses so
                                  the pipeline still runs.
    NETGRAVITY_OPENAI_API_KEY     Per-provider keys. Optional, and preferred
    NETGRAVITY_ANTHROPIC_API_KEY  over the generic one when set: keeping both
                                  in .env means flipping NETGRAVITY_USE_CLAUDE
                                  switches vendor with no other edit.
    NETGRAVITY_LLM_MODEL          Model identifier. Blank => a per-provider
                                  default from DEFAULT_MODELS below, so the
                                  model always follows the switch.
    NETGRAVITY_LLM_TIMEOUT_SECONDS  Per-request timeout (default 60).
    NETGRAVITY_LLM_MAX_RETRIES    Transient-error retries, handled by the
                                  vendor SDK (default 3).
    NETGRAVITY_LLM_STRICT         "true" => a failed live call FAILS the run.
                                  Default false => degrade to stub data, which
                                  is then labelled "[AI: FAILED -> STUB DATA]"
                                  in the report. Set true for any run whose
                                  numbers someone might act on.
    NETGRAVITY_LLM_BASE_URL       Optional. Points the OpenAI SDK at a
                                  different server instead of api.openai.com.
                                  Several providers speak the identical API
                                  ("OpenAI-compatible") — OpenRouter, Groq,
                                  Cerebras, GitHub Models — so this is the one
                                  setting that lets us use a free/alternate
                                  provider with ZERO other code changes. Put
                                  that provider's key in
                                  NETGRAVITY_OPENAI_API_KEY and its model name
                                  in NETGRAVITY_LLM_MODEL. Blank => real
                                  OpenAI, unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Repository root = three levels up from this file
# netgravity/ingestion/config.py -> netgravity/ingestion -> netgravity -> <repo root>
REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Data zones
# ---------------------------------------------------------------------------
# Borrowed from data-lake practice: raw is never edited, standardized is the
# post-mapping intermediate, curated holds immutable versioned snapshots.
# This is a naming convention today and Blob containers on Azure later.

ZONE_RAW = "raw"
ZONE_STANDARDIZED = "standardized"
ZONE_CURATED = "curated"

STUB_MODE_BANNER = (
    "AI STUB MODE — no LLM API key found; returning canned responses. "
    "Set NETGRAVITY_LLM_API_KEY to enable live extraction."
)

# Default model per provider, used when NETGRAVITY_LLM_MODEL is not set.
# Model names move fast — override with the env var rather than editing code.
DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
    # The gateway does not let a caller choose a model — it is fixed
    # server-side. This name is recorded for provenance and cost accounting
    # only; changing it here changes nothing about which model runs.
    "gateway": "gpt-5-mini",
}


def _load_dotenv_once() -> Optional[str]:
    """
    Read <repo>/.env into the environment, if present.

    Without this, `.env` is a file nobody reads: config only ever looked at
    os.environ, so a key pasted into .env would silently leave the pipeline in
    stub mode — the most confusing possible failure, because everything still
    "works".

    Real environment variables WIN over the file. That ordering matters: CI
    and Azure inject config as real env vars, and a stale committed .env must
    never quietly override them.

    python-dotenv is optional; without it we parse the handful of KEY=VALUE
    lines ourselves rather than making it a hard dependency.
    """
    path = REPO_ROOT / ".env"
    if not path.exists():
        return None
    try:
        from dotenv import load_dotenv          # type: ignore
        load_dotenv(path, override=False)
        return str(path)
    except ImportError:
        pass

    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return str(path)
    except OSError:
        return None


DOTENV_PATH = _load_dotenv_once()


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


# Per-provider key variables. Setting these lets BOTH keys live in .env at
# once, so flipping NETGRAVITY_USE_CLAUDE switches provider without also
# having to swap the key value by hand — an OpenAI key is not valid for
# Anthropic, so a single shared key variable would break on every switch.
PROVIDER_KEY_VARS = {
    "openai": "NETGRAVITY_OPENAI_API_KEY",
    "anthropic": "NETGRAVITY_ANTHROPIC_API_KEY",
    # The gateway's credential is a gateway-issued bearer token, NOT a
    # vendor API key. It gets its own variable so it can never be confused
    # with — or silently used as — an OpenAI/Anthropic key.
    "gateway": "NETGRAVITY_GATEWAY_TOKEN",
}


def resolve_provider() -> str:
    """
    Decide which provider to call. Switches only — no precedence puzzles.

        NETGRAVITY_USE_GATEWAY true        ->  gateway   (checked first)
        NETGRAVITY_USE_CLAUDE  true        ->  anthropic
        neither                            ->  openai    (default)

    The gateway is checked first because it is the most specific choice: it
    is a named internal endpoint, so anyone who deliberately switched it on
    means it, whatever else is left lying around in their .env.
    """
    if _flag("NETGRAVITY_USE_GATEWAY"):
        return "gateway"
    return "anthropic" if _flag("NETGRAVITY_USE_CLAUDE") else "openai"


def resolve_api_key(provider: str) -> Optional[str]:
    """
    Find the key for this provider.

    The provider-specific variable wins; NETGRAVITY_LLM_API_KEY remains a
    valid fallback so existing setups keep working untouched.
    """
    specific_var = PROVIDER_KEY_VARS.get((provider or "").lower())
    specific = _env(specific_var) if specific_var else None
    return specific or _env("NETGRAVITY_LLM_API_KEY")


@dataclass
class IngestionConfig:
    """Resolved configuration for one ingestion run."""

    # --- Paths ---
    data_root: Path = field(default_factory=lambda: Path(_env("NETGRAVITY_DATA_ROOT", str(REPO_ROOT / "data"))))

    # --- Storage backend ---
    storage_backend: str = field(default_factory=lambda: _env("NETGRAVITY_STORAGE_BACKEND", "local"))
    azure_connection_string: Optional[str] = field(default_factory=lambda: _env("NETGRAVITY_AZURE_CONN_STR"))

    # --- LLM ---
    # PLACEHOLDER: leave unset until the team confirms provider + key.
    # Absent key => stub mode => pipeline still runs end to end.
    # Provider comes first: the key that gets picked depends on it.
    llm_provider: str = field(default_factory=resolve_provider)
    # Left None so __post_init__ can resolve it against the chosen provider.
    # Passing it explicitly still works and is never overridden.
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = field(default_factory=lambda: _env("NETGRAVITY_LLM_MODEL"))

    # Network behaviour. A hung request must not hang the whole pipeline, and
    # a transient 429/5xx should be retried rather than degrading the run.
    llm_timeout_seconds: float = field(
        default_factory=lambda: float(_env("NETGRAVITY_LLM_TIMEOUT_SECONDS", "60") or 60)
    )
    llm_max_retries: int = field(
        default_factory=lambda: int(_env("NETGRAVITY_LLM_MAX_RETRIES", "3") or 3)
    )

    # When a LIVE call fails, do we raise or silently fall back to stub data?
    # Default False keeps a demo running; set true in any run whose numbers
    # someone might actually act on, so a failure is impossible to miss.
    llm_strict: bool = field(
        default_factory=lambda: str(_env("NETGRAVITY_LLM_STRICT", "false")).lower()
        in {"1", "true", "yes", "y"}
    )

    # Points the OpenAI SDK at a different server. Blank => real OpenAI.
    # This is what makes OpenAI-compatible free/alternate providers
    # (OpenRouter, Groq, Cerebras, GitHub Models) work with no other code
    # change: same SDK, same request shape, different address.
    llm_base_url: Optional[str] = field(
        default_factory=lambda: _env("NETGRAVITY_LLM_BASE_URL")
    )

    # Base address of the Text Generation Gateway. Used ONLY when
    # llm_provider == "gateway", and deliberately NOT llm_base_url: that one
    # points the OpenAI SDK at an OpenAI-COMPATIBLE server, and the gateway
    # is not one — it speaks its own smaller protocol (a single `prompt`
    # field, POST /v1/generate). Keeping the two apart stops a gateway
    # address from ever being handed to the OpenAI SDK.
    gateway_url: Optional[str] = field(
        default_factory=lambda: _env("NETGRAVITY_GATEWAY_URL")
    )

    # --- Behaviour ---
    # Rows failing a WARNING-level check are kept but flagged; ERROR-level rows
    # are always dropped from the assembled network.
    strict: bool = False

    def __post_init__(self) -> None:
        if self.llm_api_key is None:
            self.llm_api_key = resolve_api_key(self.llm_provider)

    @property
    def key_warning(self) -> Optional[str]:
        """
        Catch the switch-provider-but-forget-the-key trap.

        Flipping NETGRAVITY_USE_CLAUDE changes which vendor we call, but an
        OpenAI key is not valid at Anthropic and vice versa. If the only key
        present looks like it belongs to the other vendor, say so plainly —
        otherwise the run just fails with an opaque 401.
        """
        key = self.llm_api_key
        provider = (self.llm_provider or "").lower()

        if provider == "gateway":
            if not self.gateway_url:
                return ("provider is 'gateway' but NETGRAVITY_GATEWAY_URL is "
                        "not set. The gateway has no default address, so "
                        "there is nowhere to send the request.")
            if not key:
                return ("provider is 'gateway' but NETGRAVITY_GATEWAY_TOKEN "
                        "is not set — the run will fall back to stub mode.")
            return None

        if not key:
            return None
        looks_anthropic = key.startswith("sk-ant-")
        # OpenAI keys start sk- but NOT sk-ant-.
        looks_openai = key.startswith("sk-") and not looks_anthropic

        if provider == "anthropic" and looks_openai:
            return ("provider is 'anthropic' but the key looks like an OpenAI "
                    "key. Set NETGRAVITY_ANTHROPIC_API_KEY, or unset "
                    "NETGRAVITY_USE_CLAUDE to go back to OpenAI.")
        if provider == "openai" and looks_anthropic:
            return ("provider is 'openai' but the key looks like an Anthropic "
                    "key. Set NETGRAVITY_OPENAI_API_KEY, or set "
                    "NETGRAVITY_USE_CLAUDE=true to use Claude.")
        return None

    @property
    def stub_mode(self) -> bool:
        """True when no LLM key is configured — AI adapters return canned data."""
        return not bool(self.llm_api_key)

    @property
    def key_source(self) -> str:
        """Which variable supplied the key — shown in the report, never the key itself."""
        if not self.llm_api_key:
            return "none"
        specific_var = PROVIDER_KEY_VARS.get((self.llm_provider or "").lower())
        if specific_var and _env(specific_var) == self.llm_api_key:
            return specific_var
        if _env("NETGRAVITY_LLM_API_KEY") == self.llm_api_key:
            return "NETGRAVITY_LLM_API_KEY"
        return "explicit"

    @property
    def resolved_model(self) -> str:
        """
        The model to call.

        The gateway is fixed server-side — no caller-supplied model name
        can change what actually runs there, so NETGRAVITY_LLM_MODEL is
        ignored for it (it belongs to the OpenAI-compatible base-URL path
        and would otherwise mislabel every gateway call in the token
        ledger with whichever vendor model was last configured).

        For every other provider, explicit NETGRAVITY_LLM_MODEL always
        wins. Otherwise pick a sane default for the configured provider,
        so switching provider does not leave a model name from the
        previous vendor in place — which would fail with a confusing
        'unknown model' error rather than an obvious configuration one.
        """
        if (self.llm_provider or "").lower() == "gateway":
            return DEFAULT_MODELS["gateway"]
        if self.llm_model:
            return self.llm_model
        return DEFAULT_MODELS.get(
            (self.llm_provider or "").lower(), DEFAULT_MODELS["openai"]
        )

    def zone_path(self, zone: str) -> Path:
        return self.data_root / zone

    @property
    def raw_path(self) -> Path:
        return self.zone_path(ZONE_RAW)

    @property
    def standardized_path(self) -> Path:
        return self.zone_path(ZONE_STANDARDIZED)

    @property
    def curated_path(self) -> Path:
        return self.zone_path(ZONE_CURATED)

    def describe(self) -> str:
        # llm_base_url only ever affects the "openai" provider (it points the
        # OpenAI SDK at an alternate OpenAI-compatible server). Showing it
        # for "gateway" or "anthropic" makes it look like the active call is
        # going to that address when it never is -- gateway calls always go
        # to gateway_url, and anthropic calls always go to Anthropic's API.
        provider = (self.llm_provider or "").lower()
        endpoint_line: list[str] = []
        if provider == "openai" and self.llm_base_url:
            endpoint_line = [f"  API endpoint    : {self.llm_base_url}  (not api.openai.com)"]
        elif provider == "gateway":
            endpoint_line = [f"  API endpoint    : {self.gateway_url or '(not set)'}"]

        lines = [
            f"  data root       : {self.data_root}",
            f"  storage backend : {self.storage_backend}",
            f"  LLM provider    : {self.llm_provider} ({self.resolved_model})"
            + ("" if self.stub_mode else f"   key from {self.key_source}"),
        ] + endpoint_line + [
            f"  LLM mode        : {'STUB (no key set)' if self.stub_mode else 'LIVE'}"
            + ("" if self.stub_mode else
               f", timeout {self.llm_timeout_seconds:g}s, "
               f"{self.llm_max_retries} retries, "
               f"{'STRICT' if self.llm_strict else 'fallback-on-error'}"),
            f"  row strict mode : {self.strict}  (--strict: treat row warnings as failures)",
        ]
        return "\n".join(lines)


def load_config(strict: bool = False) -> IngestionConfig:
    """Build an IngestionConfig from the current environment."""
    cfg = IngestionConfig()
    cfg.strict = strict
    return cfg
