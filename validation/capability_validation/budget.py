"""
Hard ceiling on live model calls for the Phase 8.0 validation run.

The gateway's budget is SHARED and CUMULATIVE across everyone holding the
token, so an accidental loop here spends someone else's remaining capacity.
This module is the only place in the validation harness allowed to call it,
and it refuses past the limit rather than trusting callers to count.

    MAX_CALLS = 20   # for the entire validation run, per the phase brief

Every attempt is recorded — including the ones that are BLOCKED — so the
report can state what was actually spent and what was refused.

Credentials are read from the environment and never logged, never placed in a
prompt, and never included in a recorded error message.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

#: The phase brief's hard limit. Not a suggestion and not per-section.
MAX_CALLS: int = 20

#: The gateway's own shared rolling-minute limit is 20 requests. Spacing calls
#: keeps this run from consuming that whole window and 429-ing a teammate.
_MIN_SECONDS_BETWEEN_CALLS: float = 3.5

_GENERATE_PATH = "/v1/generate"
_USAGE_PATH = "/v1/usage"


@dataclass
class CallRecord:
    """One attempted live call, successful or not."""
    call_number: Optional[int]          # None when blocked (no number issued)
    capability: str
    purpose: str
    status: str                          # OK | HTTP_ERROR | TIMEOUT | BLOCKED | ERROR
    latency_seconds: Optional[float]
    prompt_chars: int
    output_chars: Optional[int]
    request_id: Optional[str]
    usage: Optional[Dict[str, Any]]
    validation: str                      # what the caller concluded about the output
    detail: str = ""


@dataclass
class LLMBudget:
    """
    Counter and gate for live gateway calls.

    `generate()` is the single entry point. It refuses once `MAX_CALLS` have
    been issued, records the refusal, and returns None so the caller can carry
    on with deterministic checks.
    """
    max_calls: int = MAX_CALLS
    calls_made: int = 0
    records: List[CallRecord] = field(default_factory=list)
    blocked: int = 0
    _last_call_at: float = 0.0

    # ------------------------------------------------------------------

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls_made)

    @property
    def configured(self) -> bool:
        """True when a token and base URL are present. Values never read out."""
        return bool(os.environ.get("TEXT_API_TOKEN") and os.environ.get("TEXT_API_URL"))

    def usage(self) -> Optional[Dict[str, Any]]:
        """Shared budget snapshot. Free — consumes no request and no money."""
        if not self.configured:
            return None
        try:
            r = requests.get(
                os.environ["TEXT_API_URL"].rstrip("/") + _USAGE_PATH,
                headers={"Authorization": f"Bearer {os.environ['TEXT_API_TOKEN']}"},
                timeout=(10, 30),
            )
            return r.json() if r.ok else {"error": f"HTTP {r.status_code}"}
        except Exception as exc:                       # noqa: BLE001
            return {"error": type(exc).__name__}

    # ------------------------------------------------------------------

    def generate(
        self,
        prompt: str,
        *,
        capability: str,
        purpose: str,
    ) -> Optional[str]:
        """
        One live prompt-completion, if budget allows.

        Returns the model's text, or None when the call was blocked or failed.
        A None return is always accompanied by a `CallRecord` explaining why —
        the harness never silently degrades a live test into a skipped one.
        """
        if not self.configured:
            self.records.append(CallRecord(
                None, capability, purpose, "BLOCKED", None, len(prompt), None,
                None, None, "not attempted", "no gateway credentials in the environment",
            ))
            self.blocked += 1
            print(f"    API_CALL BLOCKED  ({capability}) — no credentials configured")
            return None

        if self.calls_made >= self.max_calls:
            self.records.append(CallRecord(
                None, capability, purpose, "BLOCKED", None, len(prompt), None,
                None, None, "not attempted",
                f"budget exhausted at {self.max_calls}/{self.max_calls}",
            ))
            self.blocked += 1
            print(f"    API_CALL BLOCKED  {self.max_calls}/{self.max_calls} reached "
                  f"— refusing ({capability}: {purpose})")
            return None

        # Respect the gateway's shared rolling-minute window.
        gap = time.monotonic() - self._last_call_at
        if self._last_call_at and gap < _MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(_MIN_SECONDS_BETWEEN_CALLS - gap)

        self.calls_made += 1
        n = self.calls_made
        print(f"    API_CALL {n}/{self.max_calls}  {capability} — {purpose}")

        started = time.perf_counter()
        try:
            response = requests.post(
                os.environ["TEXT_API_URL"].rstrip("/") + _GENERATE_PATH,
                headers={
                    "Authorization": f"Bearer {os.environ['TEXT_API_TOKEN']}",
                    "Content-Type": "application/json",
                },
                json={"prompt": prompt},
                timeout=(10, 90),          # above the gateway's 60s processing timeout
            )
        except requests.Timeout:
            # Deliberately NOT retried: the call may have completed server-side,
            # and a retry would duplicate output and spend shared budget twice.
            self._last_call_at = time.monotonic()
            self.records.append(CallRecord(
                n, capability, purpose, "TIMEOUT",
                round(time.perf_counter() - started, 3), len(prompt), None,
                None, None, "no output", "client timeout; not retried by design",
            ))
            return None
        except Exception as exc:                        # noqa: BLE001
            self._last_call_at = time.monotonic()
            self.records.append(CallRecord(
                n, capability, purpose, "ERROR",
                round(time.perf_counter() - started, 3), len(prompt), None,
                None, None, "no output", type(exc).__name__,
            ))
            return None

        latency = round(time.perf_counter() - started, 3)
        self._last_call_at = time.monotonic()

        if not response.ok:
            body: Dict[str, Any] = {}
            try:
                body = response.json()
            except Exception:                            # noqa: BLE001
                pass
            code = str(body.get("error", ""))
            # A shared-limit refusal is an EXTERNAL constraint, not a failure of
            # the capability under test, and the two must not be reported as the
            # same thing. The gateway's daily and per-minute counters are shared
            # across every application holding this token, so they can be
            # exhausted by someone else entirely.
            external = code in {"daily_limit_exceeded", "rate_limit_exceeded",
                                "budget_exceeded", "gateway_disabled",
                                "gateway_not_configured"}
            self.records.append(CallRecord(
                n, capability, purpose,
                "EXTERNAL_LIMIT" if external else "HTTP_ERROR",
                latency, len(prompt), None,
                body.get("request_id"), None,
                "not attempted (shared limit)" if external else "no output",
                f"HTTP {response.status_code} {code}".strip(),
            ))
            if external:
                # Refunded: the gateway's own guide says a request rejected for
                # a shared limit does not count against its counters, so it must
                # not count against this run's budget either.
                self.calls_made = max(0, self.calls_made - 1)
                self.blocked += 1
                print(f"    API_CALL REFUSED by gateway ({code}); budget not "
                      f"charged, now {self.calls_made}/{self.max_calls}")
            return None

        payload = response.json()
        output = payload.get("output", "") or ""
        self.records.append(CallRecord(
            n, capability, purpose, "OK", latency, len(prompt), len(output),
            payload.get("request_id"), payload.get("usage"), "pending",
        ))
        return output

    # ------------------------------------------------------------------

    def annotate_last(self, validation: str) -> None:
        """Record what the caller concluded about the most recent output."""
        if self.records:
            self.records[-1].validation = validation

    def write(self, path: Path) -> Dict[str, Any]:
        """Persist the full call log."""
        summary = {
            "max_calls": self.max_calls,
            "calls_made": self.calls_made,
            "calls_blocked": self.blocked,
            "remaining": self.remaining,
            "successful": sum(1 for r in self.records if r.status == "OK"),
            "records": [asdict(r) for r in self.records],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary


__all__ = ["LLMBudget", "CallRecord", "MAX_CALLS"]
