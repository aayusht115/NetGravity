"""
NetGravity — LLM Client (single integration point)
===================================================
EVERY LLM call in the ingestion pipeline goes through this file.

WHY THAT MATTERS
----------------
1. API KEY PLACEHOLDER. The key is read from NETGRAVITY_LLM_API_KEY. It is
   deliberately not required. With no key set the client runs in STUB MODE
   and returns canned responses from ai/stubs.py, so the whole pipeline —
   and its entire test suite — runs end to end without credentials.
   Supplying the key later changes nothing but an environment variable.

2. PROVIDER ISOLATION. The team has not finalised the LLM provider. Swapping
   from Anthropic to anything else is a change to _call_live() in this file
   only. No adapter imports a vendor SDK directly.

3. AUDITABILITY. Every response records whether it came from a live model or
   a stub, and that flag is surfaced in the ingestion report — so nobody can
   mistake canned demo output for a real extraction.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from netgravity.ingestion.config import IngestionConfig
from netgravity.telemetry import record_call

# Providers that speak the OpenAI chat-completions API. "codex" is tolerated
# as a synonym for the same thing.
#
# NOTE: Azure OpenAI is deliberately NOT listed. It needs a different client
# class (AzureOpenAI, with azure_endpoint / api_version / azure_deployment),
# so routing it here would silently call api.openai.com instead of the Azure
# deployment. When Azure OpenAI is actually needed, it gets its own branch.
_OPENAI_PROVIDERS = {"openai", "codex"}

# Traffic logger. Everything sent to and received from a provider is
# logged here at DEBUG, so "what did we actually ask, and what came
# back" is answerable without a debugger or a code change.
#
# DEBUG, not INFO, on purpose: prompts and responses are large and can
# carry client data, so they stay off by default and are opted into.
#     logging.getLogger("netgravity.ingestion.ai.client").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

# Sentinel embedded in the note when a live call failed and we degraded to
# stub data. Adapters look for this to set FileResult.ai_failed, so the
# report can distinguish "no key configured" from "the API call broke".
LLM_FAILURE_MARKER = "LLM CALL FAILED"

# --- Text Generation Gateway ------------------------------------------------
# An internal endpoint that wraps a fixed model behind a much smaller
# protocol than OpenAI's. It is NOT OpenAI-compatible, so it cannot be
# reached by pointing the OpenAI SDK at it via base_url — it gets its own
# transport below. The whole contract:
#
#     POST {base}/v1/generate   Authorization: Bearer <token>
#     body     {"prompt": "..."}          <- exactly one field, no others
#     returns  {"output": "...", "request_id": "...", "usage": {...}}
#     GET  {base}/v1/usage      shared budget + request counters
#     GET  {base}/health        no auth, no budget cost
#
# Limits it enforces, and why each one shows up in the code below:
GATEWAY_MAX_PROMPT_CHARS = 100_000   # larger -> HTTP 413
GATEWAY_MAX_OUTPUT_TOKENS = 2_000    # fixed server-side; max_tokens is ignored

# Only these are worth retrying. Everything else is either a client mistake
# (fix it, do not repeat it) or a spent budget (retrying cannot help).
_GATEWAY_RETRYABLE_STATUS = {429, 500, 502}
# ...but not every 429. A rolling-minute limit clears on its own; a daily
# limit and an exhausted budget do not, so hammering them just burns time.
_GATEWAY_TERMINAL_ERRORS = {"daily_limit_exceeded", "budget_exceeded"}

# The gateway offers no JSON mode, so "return JSON" has to be asked for in
# the prompt itself and enforced by the parser. Appended to every gateway
# prompt; harmless if the caller's prompt already says the same thing.
_GATEWAY_JSON_INSTRUCTION = (
    "\n\nIMPORTANT: Reply with a single valid JSON object and nothing else. "
    "No prose before or after it, no markdown code fences, no explanation."
)


def _truncation_message(max_tokens: int, usage: Optional[Dict[str, int]]) -> str:
    """
    Build the "response was truncated" error, including the provider's own
    usage breakdown when we have it.

    That breakdown is the difference between a two-second guess and an
    instant diagnosis: on a reasoning model, prompt+completion can be far
    below max_tokens while total_tokens still hits it — proof the budget
    went to invisible "thinking", not to the network call failing to land.
    """
    detail = ""
    if usage:
        detail = (
            f" (usage: {usage.get('prompt_tokens', '?')} prompt + "
            f"{usage.get('completion_tokens', '?')} completion = "
            f"{usage.get('total_tokens', '?')} total — a completion count "
            f"near zero points at a reasoning/thinking budget, not a "
            f"dropped call)"
        )
    return (f"response hit the {max_tokens}-token limit and was truncated"
            f"{detail}; raise max_tokens for this call site")


def _is_unsupported_param(exc: Exception, param: str) -> bool:
    """True when an API error is specifically 'this model rejects <param>'."""
    text = str(exc).lower()
    return param.lower() in text and (
        "unsupported" in text or "unrecognized" in text
        or "not supported" in text or "unknown" in text
    )


class LLMCallError(RuntimeError):
    """A live model call failed and strict mode forbids falling back to stubs."""


@dataclass
class LLMResponse:
    """One model response, with provenance attached."""
    data: Dict[str, Any]
    stubbed: bool
    model: str
    notes: str = ""
    # True only when a LIVE call was attempted and failed. Distinguishes
    # "deliberately running without a key" from "tried to call the API and
    # could not" — the second is a problem, the first is not.
    failed: bool = False
    # The provider's raw, unparsed response text. Kept so a caller can see
    # exactly what the model said, not just what survived JSON parsing —
    # which is the difference between "the model was wrong" and "our parser
    # dropped something". Empty for stub responses.
    raw: str = ""
    # Token usage the provider reported for this call: prompt_tokens,
    # completion_tokens, total_tokens. None for stub responses, or if the
    # provider's response didn't include usage. This is what makes cost
    # visible per call instead of only on the provider's own dashboard.
    tokens: Optional[Dict[str, int]] = None

    @property
    def provenance(self) -> str:
        return "stub" if self.stubbed else self.model


class LLMClient:
    """Thin wrapper over whichever provider is configured."""

    def __init__(self, config: IngestionConfig):
        self.config = config
        self._sdk = None
        # Set by _call_openai/_call_anthropic as a side effect of the most
        # recent live call. Read by extract_json() right after _call_live()
        # returns. Not part of the public API — a transient handoff, since
        # _call_live() itself only returns text (changing that return shape
        # would touch every test that calls it directly).
        self._last_usage: Optional[Dict[str, int]] = None

    @property
    def stub_mode(self) -> bool:
        return self.config.stub_mode

    # -- public API ------------------------------------------------------

    def extract_json(self, *, task: str, prompt: str, stub_key: str,
                     stub_context: Optional[Dict[str, Any]] = None,
                     max_tokens: int = 2000) -> LLMResponse:
        """
        Ask the model for a JSON object.

        `stub_key` selects the canned response used when no API key is set,
        so each call site has a realistic offline equivalent.
        """
        if self.stub_mode:
            from netgravity.ingestion.ai import stubs
            record_call(task=task, model="stub", stubbed=True)
            return LLMResponse(
                data=stubs.get(stub_key, stub_context or {}),
                stubbed=True,
                model="stub",
                notes=f"{task}: stubbed (no API key configured)",
            )

        try:
            self._last_usage = None
            logger.debug("[%s] PROMPT SENT (%d chars):\n%s",
                         task, len(prompt), prompt)
            raw = self._call_live(prompt, max_tokens=max_tokens)
            logger.debug("[%s] RAW RESPONSE (%d chars):\n%s",
                         task, len(raw or ""), raw)
            usage = self._last_usage
            tokens_note = ""
            if usage:
                tokens_note = (
                    f" [{usage.get('total_tokens', '?')} tokens: "
                    f"{usage.get('prompt_tokens', '?')} in + "
                    f"{usage.get('completion_tokens', '?')} out]"
                )
            model_name = (f"{self.config.llm_provider}:"
                          f"{self.config.resolved_model}")
            # Parse before recording success: a parse failure here must
            # fall through to the except block below and be recorded once,
            # as a failed call -- recording success first would double-count
            # a single request in the token ledger when parsing then fails.
            data = _parse_json(raw)
            record_call(task=task, model=model_name, usage=usage)
            return LLMResponse(
                data=data,
                raw=raw,
                stubbed=False,
                model=model_name,
                notes=f"{task}: live extraction{tokens_note}",
                tokens=usage,
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.debug("[%s] CALL FAILED: %s", task, detail)

            # Recorded as a FAILED live call, not as a stub. The provider may
            # well have billed for it even though nothing usable came back —
            # counting it as a free stub would understate real spend.
            record_call(
                task=task,
                model=f"{self.config.llm_provider}:{self.config.resolved_model}",
                usage=self._last_usage, failed=True,
            )

            # In strict mode a failed call is a failed run. Silently serving
            # canned demo data in place of a real extraction is the one
            # outcome worse than stopping.
            if self.config.llm_strict:
                raise LLMCallError(
                    f"{task}: live LLM call failed and NETGRAVITY_LLM_STRICT "
                    f"is set — refusing to substitute stub data. {detail}"
                ) from exc

            # Otherwise degrade to stub so a demo survives an outage, but mark
            # it as a FAILURE, not as ordinary stub mode.
            from netgravity.ingestion.ai import stubs
            return LLMResponse(
                data=stubs.get(stub_key, stub_context or {}),
                stubbed=True,
                model="stub",
                failed=True,
                notes=f"{task}: {LLM_FAILURE_MARKER} ({detail}) — fell back to stub "
                      f"data. These numbers are NOT a real extraction. Set "
                      f"NETGRAVITY_LLM_STRICT=true to fail the run instead.",
            )

    def extract_json_from_pdf(self, *, task: str, prompt: str,
                              pdf_bytes: bytes, filename: str, stub_key: str,
                              stub_context: Optional[Dict[str, Any]] = None,
                              max_tokens: int = 2500) -> LLMResponse:
        """
        Ask the model to read a PDF DOCUMENT directly, rather than text we
        extracted from it.

        WHEN THIS IS USED
            Only as an escalation. pypdf extraction runs first because it is
            free and instant; this path exists for the cases pypdf cannot
            serve — a scan with no text layer, or a text layer so garbled
            that pdf_quality.assess() refuses it. Sending the document costs
            meaningfully more than sending text, so it is never the default.

        PROVIDER SUPPORT IS NOT UNIFORM — READ THIS
            Native PDF input is a newer capability and is NOT implemented
            identically across providers. Anthropic and OpenAI each accept a
            base64 document but in different request shapes, and several
            OpenAI-COMPATIBLE providers (OpenRouter's free tier, Groq,
            Cerebras) support it only for some models or not at all.

            CONFIRMED LIVE (2026-08-21): Gemini via its OpenAI-compatible
            endpoint (the base_url swap this project uses) REJECTS the
            OpenAI "file" content-part with a 400 "Invalid content part
            type: file". This is the one path in the whole pipeline that
            was still untested against a live provider before this — now
            it is tested, and the answer for Gemini-via-OpenAI-compat is
            "not supported". The text-only path (extract_json, no PDF) DOES
            work against Gemini and was verified separately. Anthropic's
            real API (_pdf_anthropic) natively supports PDF documents and
            is the fastest fix if a Claude key becomes available — no code
            change needed there, just NETGRAVITY_USE_CLAUDE=true.

            This method therefore treats rejection as an ordinary failure,
            not a crash: a provider that will not take a document produces
            the same loud, labelled degradation as any other failed call.
            The caller keeps the pypdf warning and reports honestly that the
            document could not be read, which is the correct outcome — far
            better than silently returning invented figures for a document
            nobody could actually read.
        """
        if self.stub_mode:
            from netgravity.ingestion.ai import stubs
            record_call(task=task, model="stub", stubbed=True)
            return LLMResponse(
                data=stubs.get(stub_key, stub_context or {}),
                stubbed=True, model="stub",
                notes=f"{task}: stubbed (no API key configured)",
            )

        try:
            self._last_usage = None
            logger.debug("[%s] PROMPT SENT with document %s (%d bytes):\n%s",
                         task, filename, len(pdf_bytes), prompt)
            raw = self._call_live_with_pdf(prompt, pdf_bytes=pdf_bytes,
                                           filename=filename,
                                           max_tokens=max_tokens)
            logger.debug("[%s] RAW RESPONSE (%d chars):\n%s",
                         task, len(raw or ""), raw)
            usage = self._last_usage
            tokens_note = ""
            if usage:
                tokens_note = (
                    f" [{usage.get('total_tokens', '?')} tokens: "
                    f"{usage.get('prompt_tokens', '?')} in + "
                    f"{usage.get('completion_tokens', '?')} out]"
                )
            model_name = (f"{self.config.llm_provider}:"
                          f"{self.config.resolved_model}")
            # Parse before recording success -- see extract_json() for why.
            data = _parse_json(raw)
            record_call(task=task, model=model_name, usage=usage)
            return LLMResponse(
                data=data, raw=raw, stubbed=False,
                model=model_name,
                notes=f"{task}: live document read{tokens_note}",
                tokens=usage,
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            record_call(
                task=task,
                model=f"{self.config.llm_provider}:{self.config.resolved_model}",
                usage=self._last_usage, failed=True,
            )
            if self.config.llm_strict:
                raise LLMCallError(
                    f"{task}: reading the PDF directly failed and "
                    f"NETGRAVITY_LLM_STRICT is set — refusing to substitute "
                    f"stub data. {detail}"
                ) from exc
            from netgravity.ingestion.ai import stubs
            return LLMResponse(
                data=stubs.get(stub_key, stub_context or {}),
                stubbed=True, model="stub", failed=True,
                notes=(f"{task}: {LLM_FAILURE_MARKER} reading the PDF directly "
                       f"({detail}). The provider may not support document "
                       f"input for this model. These numbers are NOT a real "
                       f"extraction."),
            )

    def _call_live_with_pdf(self, prompt: str, *, pdf_bytes: bytes,
                            filename: str, max_tokens: int) -> str:
        """Route a document read to the configured provider."""
        import base64

        encoded = base64.b64encode(pdf_bytes).decode("ascii")
        provider = (self.config.llm_provider or "").lower()

        if provider in _OPENAI_PROVIDERS:
            return self._pdf_openai(prompt, encoded=encoded, filename=filename,
                                    max_tokens=max_tokens)
        if provider == "anthropic":
            return self._pdf_anthropic(prompt, encoded=encoded,
                                       max_tokens=max_tokens)
        if provider == "gateway":
            raise NotImplementedError(
                "The Text Generation Gateway accepts text prompts only — it "
                "has no document/PDF input at all. Extract the text first "
                "and send that instead."
            )
        raise NotImplementedError(
            f"Reading a PDF directly is not implemented for provider "
            f"'{self.config.llm_provider}'."
        )

    def _pdf_openai(self, prompt: str, *, encoded: str, filename: str,
                    max_tokens: int) -> str:
        if self._sdk is None:
            from openai import OpenAI
            self._sdk = OpenAI(
                api_key=self.config.llm_api_key,
                base_url=self.config.llm_base_url,
                timeout=self.config.llm_timeout_seconds,
                max_retries=self.config.llm_max_retries,
            )
        content = [
            {"type": "text", "text": prompt},
            {"type": "file",
             "file": {"filename": filename,
                      "file_data": f"data:application/pdf;base64,{encoded}"}},
        ]
        kwargs: Dict[str, Any] = {
            "model": self.config.resolved_model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
        }
        if self._looks_like_gemini():
            kwargs["reasoning_effort"] = "none"
        resp = self._sdk.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._last_usage = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        choice = resp.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise ValueError(_truncation_message(max_tokens, self._last_usage))
        return choice.message.content or ""

    def _pdf_anthropic(self, prompt: str, *, encoded: str,
                       max_tokens: int) -> str:
        if self._sdk is None:
            from anthropic import Anthropic
            self._sdk = Anthropic(
                api_key=self.config.llm_api_key,
                timeout=self.config.llm_timeout_seconds,
                max_retries=self.config.llm_max_retries,
            )
        message = self._sdk.messages.create(
            model=self.config.resolved_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": encoded}},
                {"type": "text", "text": prompt},
            ]}],
        )
        usage = getattr(message, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None)
            self._last_usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": (input_tokens + output_tokens)
                if input_tokens is not None and output_tokens is not None else None,
            }
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise ValueError(_truncation_message(max_tokens, self._last_usage))
        return "".join(b.text for b in message.content
                       if getattr(b, "type", None) == "text")

    # -- provider-specific ------------------------------------------------

    def _call_live(self, prompt: str, *, max_tokens: int) -> str:
        """
        The ONLY provider-specific code in the ingestion package.
        Adding a provider means adding a branch here and nothing else.

        Both SDKs handle their own retry/backoff on transient errors (429,
        5xx) when given max_retries, so we do not hand-roll a retry loop.
        """
        provider = (self.config.llm_provider or "openai").lower()

        if provider in _OPENAI_PROVIDERS:
            return self._call_openai(prompt, max_tokens=max_tokens)
        if provider == "anthropic":
            return self._call_anthropic(prompt, max_tokens=max_tokens)
        if provider == "gateway":
            return self._call_gateway(prompt, max_tokens=max_tokens)

        raise NotImplementedError(
            f"LLM provider '{provider}' is not wired up. Supported: "
            f"{', '.join(sorted(_OPENAI_PROVIDERS | {'anthropic', 'gateway'}))}. "
            f"Add a branch in LLMClient._call_live()."
        )

    # -- Text Generation Gateway -------------------------------------------

    def _call_gateway(self, prompt: str, *, max_tokens: int) -> str:
        """
        Call the Text Generation Gateway.

        Hand-rolled over urllib rather than an SDK, because the gateway has
        no SDK and adding an HTTP library for one endpoint would be a new
        dependency for four lines of work.

        WHAT IS DIFFERENT ABOUT THIS PROVIDER
            - No JSON mode. "Return JSON" is asked for in the prompt and
              enforced by _parse_json(), which already tolerates prose and
              fences — so a chatty reply still parses.
            - No model choice, and max_tokens is ignored (fixed at
              GATEWAY_MAX_OUTPUT_TOKENS server-side). A caller asking for
              more is warned rather than silently truncated.
            - No document/PDF input at all.
            - A SHARED, CUMULATIVE budget. Retries are therefore deliberately
              conservative: a rolling-minute 429 is worth waiting out, but a
              spent budget or daily cap is not, and a client-side timeout is
              never retried because the call may already have completed and
              been billed (there is no idempotency key to protect us).
        """
        base = (self.config.gateway_url or "").rstrip("/")
        if not base:
            raise RuntimeError(
                "provider is 'gateway' but no gateway address is set. Set "
                "NETGRAVITY_GATEWAY_URL."
            )

        # GUARD AGAINST A LEFTOVER VENDOR URL.
        # NETGRAVITY_LLM_BASE_URL commonly holds a vendor endpoint (Gemini's
        # OpenAI-compatible address, OpenRouter, ...). If one of those ends
        # up here, the gateway's request shape would be POSTed to a vendor
        # that does not speak it — producing a confusing 4xx that looks like
        # a gateway fault rather than a configuration mistake. Name it.
        if _looks_like_vendor_endpoint(base):
            raise RuntimeError(
                f"the gateway address points at a model vendor ({base}). "
                f"That is almost certainly a leftover NETGRAVITY_LLM_BASE_URL "
                f"value — the gateway is a separate internal endpoint and "
                f"does not speak the vendor API. Set NETGRAVITY_GATEWAY_URL "
                f"to the gateway's own address."
            )
        if not self.config.llm_api_key:
            raise RuntimeError(
                "provider is 'gateway' but NETGRAVITY_GATEWAY_TOKEN is not set."
            )

        if max_tokens > GATEWAY_MAX_OUTPUT_TOKENS:
            logger.debug(
                "gateway ignores max_tokens=%d; its output is capped at %d",
                max_tokens, GATEWAY_MAX_OUTPUT_TOKENS)

        body = prompt + _GATEWAY_JSON_INSTRUCTION
        if len(body) > GATEWAY_MAX_PROMPT_CHARS:
            # Caught here rather than as a 413, so the message names the real
            # problem and no request (or budget) is spent discovering it.
            raise ValueError(
                f"prompt is {len(body):,} characters, above the gateway's "
                f"{GATEWAY_MAX_PROMPT_CHARS:,} limit. Shorten the input "
                f"before sending."
            )

        payload = json.dumps({"prompt": body}).encode("utf-8")
        request = urllib.request.Request(
            f"{base}/v1/generate", data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {self.config.llm_api_key}",
                "Content-Type": "application/json",
            },
        )

        attempts = max(1, self.config.llm_max_retries)
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(
                        request,
                        timeout=self.config.llm_timeout_seconds) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                error, message = _read_gateway_error(exc)
                retryable = (exc.code in _GATEWAY_RETRYABLE_STATUS
                             and error not in _GATEWAY_TERMINAL_ERRORS)
                if retryable and attempt < attempts - 1:
                    # Exponential backoff with jitter. The jitter matters
                    # because the limits are SHARED: without it, several
                    # callers throttled at the same moment would retry in
                    # lockstep and throttle each other again.
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    logger.debug("gateway %s (%s) — retrying in %.1fs",
                                 exc.code, error, delay)
                    time.sleep(delay)
                    continue
                raise RuntimeError(_gateway_failure_text(exc.code, error,
                                                         message)) from exc
            except urllib.error.URLError as exc:
                # Includes client-side timeouts. NOT retried on purpose: the
                # request may have been processed and billed, and the gateway
                # accepts no idempotency key, so a retry risks paying twice
                # for one answer.
                raise RuntimeError(
                    f"could not reach the gateway at {base}: {exc.reason}. "
                    f"Not retried — the call may already have been processed "
                    f"and billed."
                ) from exc

        usage = data.get("usage") or {}
        self._last_usage = {
            # The gateway names these differently from OpenAI. Translating
            # here keeps every downstream consumer provider-agnostic.
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        if data.get("request_id"):
            logger.debug("gateway request_id=%s", data["request_id"])

        output = data.get("output")
        if not output:
            raise ValueError(
                f"gateway returned no output (request_id="
                f"{data.get('request_id', 'unknown')})."
            )
        return output

    # -- OpenAI ------------------------------------------------------------

    def _looks_like_gemini(self) -> bool:
        base_url = self.config.llm_base_url or ""
        model = self.config.resolved_model or ""
        return "generativelanguage.googleapis.com" in base_url \
            or model.startswith("gemini")

    def _create_chat_completion(self, prompt: str, max_tokens: int):
        """
        Call chat.completions.create(), degrading two independent optional
        features one at a time on the specific error that says a provider
        doesn't support them — rather than assuming every OpenAI-compatible
        provider (OpenRouter, Groq, Cerebras, GitHub Models, ...) supports
        every OpenAI feature identically:

          - JSON mode (response_format): guarantees syntactically valid
            JSON back. Real OpenAI models support it; some alternate
            providers/models don't and reject the parameter.
          - max_completion_tokens vs max_tokens: newer OpenAI models renamed
            this and reject the old name; most other providers still expect
            the old name.

        At most 4 attempts, each one only ever removing a param that was
        just rejected, so this always terminates.
        """
        base: Dict[str, Any] = {
            "model": self.config.resolved_model,
            "messages": [{"role": "user", "content": prompt}],
        }

        # Gemini (via Google's OpenAI-compatible endpoint) is a reasoning
        # model: by default it spends part of max_tokens on invisible
        # "thinking" before writing the visible answer. A small max_tokens
        # (our own handshake call asks for only 20) can be entirely consumed
        # by thinking, so the call comes back truncated with NO visible
        # content — indistinguishable at a glance from the call never
        # reaching the provider at all. reasoning_effort="none" turns
        # thinking off. Scoped to Gemini specifically (by base_url or model
        # name) since not every provider recognises this parameter the same
        # way, and it is meaningless for a non-reasoning model like
        # gpt-4o-mini.
        if self._looks_like_gemini():
            base["reasoning_effort"] = "none"

        use_json_mode = True
        token_param = "max_completion_tokens"

        for _ in range(4):
            kwargs = dict(base)
            if use_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            kwargs[token_param] = max_tokens
            try:
                return self._sdk.chat.completions.create(**kwargs)
            except Exception as exc:
                if token_param == "max_completion_tokens" and \
                        _is_unsupported_param(exc, "max_completion_tokens"):
                    token_param = "max_tokens"
                    continue
                if use_json_mode and _is_unsupported_param(exc, "response_format"):
                    use_json_mode = False
                    continue
                raise
        raise RuntimeError("unreachable: fallback loop exhausted")  # pragma: no cover

    def _call_openai(self, prompt: str, *, max_tokens: int) -> str:
        if self._sdk is None:
            try:
                from openai import OpenAI   # lazy: not needed in stub mode
            except ImportError as exc:
                raise ImportError(
                    "The 'openai' package is required for live extraction with "
                    "this provider. Install it with `pip install openai`."
                ) from exc
            self._sdk = OpenAI(
                api_key=self.config.llm_api_key,
                # None => SDK default (api.openai.com). Set this to point at
                # any OpenAI-compatible server instead — OpenRouter, Groq,
                # Cerebras, GitHub Models — with no other code change.
                base_url=self.config.llm_base_url,
                timeout=self.config.llm_timeout_seconds,
                max_retries=self.config.llm_max_retries,
            )

        resp = self._create_chat_completion(prompt, max_tokens)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._last_usage = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        choice = resp.choices[0]
        # A truncated response is invalid JSON and would fail parsing with a
        # confusing error. Name the real cause instead.
        if getattr(choice, "finish_reason", None) == "length":
            raise ValueError(_truncation_message(max_tokens, self._last_usage))
        return choice.message.content or ""

    # -- Anthropic ---------------------------------------------------------

    def _call_anthropic(self, prompt: str, *, max_tokens: int) -> str:
        if self._sdk is None:
            try:
                from anthropic import Anthropic   # lazy: not needed in stub mode
            except ImportError as exc:
                raise ImportError(
                    "The 'anthropic' package is required for live extraction "
                    "with this provider. Install it with `pip install anthropic`."
                ) from exc
            self._sdk = Anthropic(
                api_key=self.config.llm_api_key,
                timeout=self.config.llm_timeout_seconds,
                max_retries=self.config.llm_max_retries,
            )

        message = self._sdk.messages.create(
            model=self.config.resolved_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = getattr(message, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None)
            self._last_usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": (input_tokens + output_tokens)
                if input_tokens is not None and output_tokens is not None else None,
            }
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise ValueError(_truncation_message(max_tokens, self._last_usage))
        return "".join(
            block.text for block in message.content
            if getattr(block, "type", None) == "text"
        )


_VENDOR_HOST_MARKERS = (
    "openai.com", "anthropic.com", "googleapis.com", "openrouter.ai",
    "groq.com", "cerebras.ai", "inference.ai.azure.com",
)


def _looks_like_vendor_endpoint(url: str) -> bool:
    """True when a URL belongs to a model vendor rather than our gateway."""
    lowered = url.lower()
    return any(marker in lowered for marker in _VENDOR_HOST_MARKERS)


def _read_gateway_error(exc: urllib.error.HTTPError) -> tuple:
    """
    Pull (error_code, message) out of a gateway error body.

    The gateway returns structured JSON errors, but auth and validation
    failures can happen before that body is built — so this never assumes
    the body is parseable, and degrades to the raw text instead.
    """
    try:
        body = json.loads(exc.read().decode("utf-8"))
        return body.get("error", ""), body.get("message", "")
    except Exception:
        return "", ""


def _gateway_failure_text(status: int, error: str, message: str) -> str:
    """
    Turn a gateway error into something that says what to DO about it.

    The status code alone is not actionable — 429 could mean "wait a minute",
    "wait until tomorrow", or "the shared budget is gone and no amount of
    waiting will help". Those need different responses from whoever reads
    the log, so they get different text.
    """
    guidance = {
        "invalid_request": "the request was malformed — do not retry it unchanged.",
        "invalid_api_key": "NETGRAVITY_GATEWAY_TOKEN is missing or wrong.",
        "not_found": "the URL is wrong — check NETGRAVITY_GATEWAY_URL.",
        "method_not_allowed": "wrong HTTP method for this endpoint.",
        "prompt_too_large": "the prompt exceeded the gateway's size limit.",
        "rate_limit_exceeded": ("the shared per-minute limit was hit and "
                                "retries were exhausted. Wait and run again."),
        "daily_limit_exceeded": ("the shared DAILY request limit is spent. It "
                                 "resets at 00:00 UTC — not retried."),
        "budget_exceeded": ("the shared cost budget is exhausted. This is "
                            "cumulative and does NOT reset daily; someone has "
                            "to raise it. Not retried."),
        "gateway_disabled": "the gateway is switched off server-side.",
        "gateway_not_configured": "the gateway is not configured server-side.",
    }.get(error, "")

    parts = [f"gateway call failed with HTTP {status}"]
    if error:
        parts.append(f"({error})")
    if message:
        parts.append(f"— {message}")
    if guidance:
        parts.append(f" {guidance}")
    return " ".join(parts)


def fetch_gateway_usage(config: IngestionConfig) -> Dict[str, Any]:
    """
    Read the gateway's shared budget and request counters.

    Worth calling BEFORE a batch run. The budget is shared with everyone
    holding the same credential and is cumulative — it does not reset daily
    — so "how much is left" is a question with a real answer that is cheap
    to ask. This endpoint costs no budget and no request quota.
    """
    base = (config.gateway_url or "").rstrip("/")
    if not base:
        raise RuntimeError("NETGRAVITY_GATEWAY_URL is not set.")
    request = urllib.request.Request(
        f"{base}/v1/usage",
        headers={"Authorization": f"Bearer {config.llm_api_key or ''}"},
    )
    with urllib.request.urlopen(
            request, timeout=config.llm_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_json(text: str) -> Dict[str, Any]:
    """Pull a JSON object out of a model response, tolerating prose or fences."""
    text = text.strip()

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        first, last = text.find("{"), text.rfind("}")
        if first != -1 and last > first:
            text = text[first:last + 1]

    return json.loads(text)


def get_client(config: IngestionConfig) -> LLMClient:
    return LLMClient(config)
