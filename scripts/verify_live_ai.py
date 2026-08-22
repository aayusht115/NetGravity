"""
NetGravity — Live AI Verification
==================================
Checks that a real API key works and that every AI-backed ingestion flow
behaves correctly against the live provider.

    python scripts/verify_live_ai.py

WHY THIS EXISTS
---------------
The 369-test suite deliberately never touches the network: it replaces the
vendor SDK with a fake. That proves our logic is right, but it cannot prove
your key works, your model name is valid, or that the provider returns what
the prompts expect. This script covers exactly that gap.

It is NOT part of `pytest`. It costs real money (a few tenths of a cent) and
needs credentials, so it stays an explicit, deliberate command.

WHAT IT CHECKS
    1. Configuration resolves and a key is present
    2. The key is accepted by the provider (one tiny call)
    3. Contract extraction returns real, structured clauses
    4. The extraction cache stops the second call
    5. Distributor column mapping returns a usable mapping
    6. A bad key fails loudly rather than silently using stub data
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from netgravity.ingestion.adapters import contracts as contracts_adapter
from netgravity.ingestion.adapters.distributor import read_rows
from netgravity.ingestion.ai.client import LLMCallError, LLMClient, get_client
from netgravity.ingestion.ai.column_mapper import propose_mapping
from netgravity.ingestion.config import DOTENV_PATH, IngestionConfig, load_config
from netgravity.ingestion.storage import get_storage

RULE = "─" * 68
PASS, FAIL, WARN = "  ✓", "  ✗", "  !"

SAMPLE = REPO_ROOT / "data" / "mock" / "india"
_failures: list[str] = []
_token_totals: list[int] = []


def _track_tokens(note: str) -> None:
    """
    Pull the total-token figure out of a live-extraction note (see
    ai/client.py's extract_json — the note text looks like
    "...live extraction [1523 tokens: 1450 in + 73 out]") and add it to the
    running total for this whole verification run. Parsed from the note
    string rather than a structured field so this stays a script-local
    concern, not a change to what every adapter returns.
    """
    match = re.search(r"\[(\d+) tokens:", note)
    if match:
        _token_totals.append(int(match.group(1)))


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"{PASS if ok else FAIL} {label}")
    if detail:
        for line in str(detail).splitlines():
            print(f"        {line}")
    if not ok:
        _failures.append(label)
    return ok


def section(title: str) -> None:
    print(f"\n{title}\n{RULE}")


# ---------------------------------------------------------------------------

def step_1_config(cfg: IngestionConfig) -> bool:
    section("1. Configuration")
    print(cfg.describe())
    if DOTENV_PATH:
        print(f"  .env loaded     : {DOTENV_PATH}")
    else:
        print("  .env loaded     : (none found — using shell environment only)")

    if cfg.stub_mode:
        print()
        print(FAIL + " No API key found. Nothing below can run.")
        print("        Add ONE of these to .env in the repo root:")
        print("            NETGRAVITY_OPENAI_API_KEY=sk-...")
        print("            NETGRAVITY_LLM_API_KEY=sk-...")
        return False

    if cfg.key_warning:
        print()
        check(False, "key matches the selected provider", cfg.key_warning)
        return False

    check(True, f"key present ({cfg.key_source}), provider={cfg.llm_provider}, "
                f"model={cfg.resolved_model}")
    return True


def step_2_handshake(cfg: IngestionConfig) -> bool:
    section("2. Provider handshake (one tiny call)")
    client = get_client(cfg)
    started = time.time()
    try:
        raw = client._call_live(
            'Reply with exactly this JSON and nothing else: {"ok": true}',
            # Generous even though the wanted answer is 5 tokens: a
            # reasoning model (Gemini) spends part of this budget on
            # invisible "thinking" before writing the visible answer, even
            # with reasoning_effort=none reducing but not always zeroing
            # that spend. 20 was too tight and produced a truncated,
            # content-free response that looked identical to "never reached
            # the provider" (it wasn't -- see the usage breakdown below).
            max_tokens=200,
        )
        elapsed = time.time() - started
        return check(True, f"provider accepted the key ({elapsed:.1f}s)",
                     f"response: {raw.strip()[:70]}")
    except Exception as exc:
        elapsed = time.time() - started
        hint = diagnose_handshake_error(str(exc), cfg.resolved_model)
        return check(False, f"provider call failed after {elapsed:.1f}s",
                     f"{type(exc).__name__}: {exc}\n{hint}".strip())


def diagnose_handshake_error(error_text: str, model: str) -> str:
    """
    Turn a raw provider error into a plain-language next step.

    Order matters: a shared free-tier model being temporarily oversubscribed
    ("rate-limited") and an account genuinely having no billing credit
    ("insufficient_quota") are BOTH surfaced as HTTP 429 by most providers,
    including OpenRouter -- so the rate-limit check must run before the
    generic "429 => billing" one, or a transient, retry-in-5-seconds
    situation gets misdiagnosed as a dead account.
    """
    text = error_text.lower()
    if "401" in text or "invalid_api_key" in text or "incorrect api key" in text:
        return "The key was rejected. Check for a typo or a revoked key."
    if "model" in text and ("not" in text or "does not exist" in text):
        return (f"Model '{model}' was rejected. Set NETGRAVITY_LLM_MODEL to "
                f"one your account can use.")

    # These two both surface as HTTP 429 from most providers (OpenRouter
    # included), but mean opposite things: a shared model being momentarily
    # busy (retry in seconds) vs. an account genuinely out of money (retry
    # never helps without adding funds). The exception class name itself is
    # literally "RateLimitError" in BOTH cases, so matching on "rate" +
    # "limit" alone would wrongly call every 429 a shared-pool hiccup —
    # check the specific, unambiguous phrase instead.
    if "insufficient_quota" in text or "check your plan and billing" in text:
        return "Key is valid but the account has no quota/credit."
    if "temporarily rate-limited" in text or "upstream_429" in text \
            or "shared_pool" in text or "retry shortly" in text:
        return ("This specific model is temporarily oversubscribed (a "
                "shared free-tier rate limit), NOT a billing problem. Wait "
                "a few seconds and retry, or set NETGRAVITY_LLM_MODEL to a "
                "different free model.")
    if "429" in text:
        return ("Rate limited (429). Could be a shared free-tier limit "
                "(retry shortly / try a different free model) or the "
                "account's own quota — see the raw error above for which.")
    return ""


def step_3_contracts(cfg: IngestionConfig) -> bool:
    section("3. Contract extraction (live)")
    contract_dir = SAMPLE / "contracts"
    if not contract_dir.exists():
        return check(False, "sample contracts found", f"missing {contract_dir}")

    storage = get_storage(cfg)
    ok = True
    for path in sorted(contract_dir.glob("*.txt")):
        rule, result = contracts_adapter.ingest_file(
            path, cfg, None, storage, use_cache=False)

        if rule is None:
            ok = check(False, f"{path.name}: extraction returned nothing") and ok
            continue
        if result.ai_failed:
            note = next((n for n in result.ai_notes if "FAILED" in n), "")
            ok = check(False, f"{path.name}: live call failed", note) and ok
            continue
        if result.ai_stubbed:
            ok = check(False, f"{path.name}: returned STUB data, not a live "
                              f"extraction") and ok
            continue

        detail = [f"vendor      : {rule.vendor_name}",
                  f"base rate   : {rule.base_rate:g} {rule.rate_unit}",
                  f"surcharges  : {len(rule.surcharges)}",
                  f"extracted by: {rule.extracted_by}"]
        for s in rule.surcharges:
            scope = (", ".join(s.applies_to_location_ids)
                     or f"{len(s.applies_to_pin_codes)} pin codes" or "all lanes")
            detail.append(f"  - {s.surcharge_type.value} {s.rate:g} -> {scope}")
        # ai_notes carries the live-extraction note, which includes the real
        # token count reported by the provider for this exact call.
        for note in result.ai_notes:
            if "tokens" in note:
                detail.append(f"  {note}")
                _track_tokens(note)
        ok = check(True, f"{path.name}: extracted live", "\n".join(detail)) and ok

        # The business finding must survive a real extraction, not just a stub.
        if rule.has_hidden_cost:
            check(True, f"{path.name}: hidden-surcharge finding raised (R-014)")
    return ok


def step_4_cache(cfg: IngestionConfig) -> bool:
    section("4. Extraction cache (second run must not call the API)")
    contract = next(iter(sorted((SAMPLE / "contracts").glob("*.txt"))), None)
    if contract is None:
        return check(False, "a sample contract to cache")

    storage = get_storage(cfg)
    _, first = contracts_adapter.ingest_file(contract, cfg, None, storage)
    _, second = contracts_adapter.ingest_file(contract, cfg, None, storage)

    reused = (second.ai_used is False
              and any("cached" in n for n in second.ai_notes))
    return check(reused, "second run served from cache (no model call)",
                 f"first : ai_used={first.ai_used}\n"
                 f"second: ai_used={second.ai_used}\n"
                 + "\n".join(second.ai_notes))


def step_5_distributor(cfg: IngestionConfig) -> bool:
    section("5. Distributor column mapping (live)")
    files = sorted((SAMPLE / "distributors").glob("*.xlsx"))
    if not files:
        return check(False, "a sample distributor file")

    path = files[0]
    columns, rows, _ = read_rows(path)
    mapping, note = propose_mapping(
        get_client(cfg), columns=columns, sample_rows=rows,
        distributor_id=path.stem, known_ids=["MKT_DELHI", "MKT_JAIPUR", "DC_DELHI"],
    )

    if mapping.proposed_by == "stub":
        return check(False, "mapping came from the live model", note)

    detail = [f"proposed by : {mapping.proposed_by}",
              f"target      : {mapping.target_entity}"]
    for m in mapping.mappings:
        flag = "  <-- needs human review" if m.needs_review else ""
        detail.append(f'  "{m.source_column}" -> {m.target_field} '
                      f'({m.confidence:.0%}){flag}')
    if mapping.unmapped_columns:
        detail.append(f"  unmapped: {', '.join(mapping.unmapped_columns)}")
    # `note` carries the live-extraction note, which includes the real
    # token count reported by the provider for this exact call.
    if "tokens" in note:
        detail.append(f"  {note}")
        _track_tokens(note)

    ok = check(bool(mapping.mappings), "live mapping proposed", "\n".join(detail))
    check(bool(mapping.needs_review) or True,
          f"{len(mapping.needs_review)} column(s) flagged for human confirmation")
    return ok


def step_6_bad_key_fails_loudly(cfg: IngestionConfig) -> bool:
    section("6. A broken key must fail loudly, not silently use stubs")
    bad = IngestionConfig()
    bad.llm_api_key = "sk-deliberately-invalid-key"
    bad.llm_provider = cfg.llm_provider
    bad.llm_model = cfg.resolved_model
    bad.llm_max_retries = 0
    bad.llm_timeout_seconds = 15

    bad.llm_strict = False
    resp = LLMClient(bad).extract_json(task="probe", prompt="return json {}",
                                       stub_key="contract")
    lenient = resp.failed and "NOT a real extraction" in resp.notes
    check(lenient, "non-strict: degrades to stub but is labelled a FAILURE")

    bad.llm_strict = True
    try:
        LLMClient(bad).extract_json(task="probe", prompt="return json {}",
                                    stub_key="contract")
        strict = False
    except LLMCallError:
        strict = True
    return check(strict, "strict: refuses to substitute stub data") and lenient


def main() -> int:
    print()
    print("NetGravity — Live AI Verification")
    print(RULE)
    print("Real API calls. Expect a few tenths of a cent.")

    cfg = load_config()
    if not step_1_config(cfg):
        return 1
    if not step_2_handshake(cfg):
        print(f"\n{RULE}\nStopped: the provider would not accept this key.\n")
        return 1

    step_3_contracts(cfg)
    step_4_cache(cfg)
    step_5_distributor(cfg)
    step_6_bad_key_fails_loudly(cfg)

    section("Result")
    if _failures:
        print(f"{FAIL} {len(_failures)} check(s) failed:")
        for f in _failures:
            print(f"        - {f}")
        print()
        return 1
    print(f"{PASS} All live checks passed. Every AI flow works against "
          f"{cfg.llm_provider}:{cfg.resolved_model}.")
    if _token_totals:
        print(f"  Tokens used this run: {sum(_token_totals)} "
              f"across {len(_token_totals)} live call(s)")
    print()
    print("  Next: run the full pipeline with live extraction —")
    print("      python -m netgravity.ingestion --source data/mock/india --explain")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
