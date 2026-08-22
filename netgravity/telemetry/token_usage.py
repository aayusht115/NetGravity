"""
NetGravity — Token Usage Ledger (the ONE place AI spend is counted)
====================================================================
Every model call anywhere in NetGravity records here. Ingestion today;
scenarios, resilience, or anything else that calls a model tomorrow uses
the same three lines and appears in the same totals.

WHY THIS EXISTS AS ITS OWN MODULE
---------------------------------
Token spend is the one number that is invisible until it is a problem.
A provider dashboard tells you the monthly total; it does not tell you
that ONE step of ONE run burned 80% of it. Recording per call, with the
task name attached, turns "the bill is high" into "contract extraction on
14 PDFs is the expensive part" — which is actionable.

It sits OUTSIDE netgravity/ingestion/ on purpose. Cost is a property of
the run, not of the subsystem, so a per-subsystem counter would have to be
manually summed by whoever wants the real total — and would silently miss
any new caller that forgot to add itself.

HOW TO USE IT (the whole API)
-----------------------------
Recording, from any caller::

    from netgravity.telemetry import record_call

    record_call(task="contract extraction", model="openai:gpt-4o-mini",
                usage={"prompt_tokens": 900, "completion_tokens": 120,
                       "total_tokens": 1020})

Reading, at the end of a run::

    from netgravity.telemetry import ledger
    print(ledger().summary())

That is the entire contract. Nothing else needs to change to make a new
call site appear in the totals.

TWO DELIBERATE GUARANTEES
-------------------------
1. RECORDING NEVER BREAKS A RUN. Every entry point swallows its own
   errors. Accounting is a bystander to the work — a malformed usage dict
   must never take down an extraction that otherwise succeeded.

2. STUB AND FAILED CALLS ARE COUNTED SEPARATELY, NOT SILENTLY.
   A stub call costs nothing and must never inflate a cost estimate; a
   FAILED live call may well have been billed by the provider even though
   it returned nothing useful. Both are tracked as their own counts so a
   summary can never read as "cheap!" when the truth is "half the calls
   failed and we still paid for some of them".
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
# USD per 1,000,000 tokens, as (input, output).
#
# THESE ARE ESTIMATES FOR LOCAL VISIBILITY, NOT AN INVOICE. Provider prices
# change without notice and vary by region and tier, so every figure derived
# from this table is reported as approximate and clearly marked. The
# provider's own billing is always the authority.
#
# A model that is not listed is NOT guessed at — cost comes back None and
# the summary says the token count is known but the cost is not, which is
# honest. Guessing a rate would produce a confident wrong number, which is
# the failure mode this whole codebase is built to avoid.
#
# Override or extend at runtime without touching code:
#     NETGRAVITY_TOKEN_PRICES="my-model:0.25:2.00,other:1.0:3.0"
_DEFAULT_PRICES: Dict[str, tuple] = {
    # Rates recorded from the gateway integration guide supplied 2026-08-20.
    "gpt-5-mini": (0.25, 2.00),
}


def _load_price_overrides() -> Dict[str, tuple]:
    """Parse NETGRAVITY_TOKEN_PRICES. A malformed entry is skipped, not fatal."""
    raw = os.environ.get("NETGRAVITY_TOKEN_PRICES", "").strip()
    if not raw:
        return {}
    prices: Dict[str, tuple] = {}
    for chunk in raw.split(","):
        parts = chunk.strip().split(":")
        if len(parts) != 3:
            continue
        name, in_rate, out_rate = parts
        try:
            prices[name.strip().lower()] = (float(in_rate), float(out_rate))
        except ValueError:
            continue
    return prices


def _price_for(model: str) -> Optional[tuple]:
    """
    Find the rate for a model name, tolerating the decorations real model
    strings carry: a 'provider:model' prefix, a dated suffix, a vendor path.
    Longest match wins so 'gpt-5-mini-2026-01' does not accidentally match a
    shorter, cheaper 'gpt-5' entry.
    """
    if not model:
        return None
    table = dict(_DEFAULT_PRICES)
    table.update(_load_price_overrides())

    needle = model.lower()
    if ":" in needle:                 # "openai:gpt-4o-mini" -> "gpt-4o-mini"
        needle = needle.split(":", 1)[1]
    if "/" in needle:                 # "openai/gpt-4o-mini" -> "gpt-4o-mini"
        needle = needle.rsplit("/", 1)[1]

    if needle in table:
        return table[needle]
    matches = [name for name in table if name in needle]
    return table[max(matches, key=len)] if matches else None


def estimate_cost_usd(model: str, prompt_tokens: Optional[int],
                      completion_tokens: Optional[int]) -> Optional[float]:
    """
    Approximate USD cost of one call, or None when the rate is unknown.

    None is a real answer here and is rendered as "cost unknown" rather than
    as zero. Zero would understate a bill; None states plainly that the
    token count is known and the price is not.
    """
    rates = _price_for(model)
    if rates is None or prompt_tokens is None or completion_tokens is None:
        return None
    in_rate, out_rate = rates
    return (prompt_tokens / 1_000_000) * in_rate + \
           (completion_tokens / 1_000_000) * out_rate


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenUsage:
    """Token counts for one call. Any field may be None — providers differ."""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "TokenUsage":
        if not data:
            return cls()

        def _as_int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        prompt = _as_int(data.get("prompt_tokens"))
        completion = _as_int(data.get("completion_tokens"))
        total = _as_int(data.get("total_tokens"))
        # Some providers report only the parts, others only the total.
        # Fill in whichever is derivable rather than reporting a hole.
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        return cls(prompt_tokens=prompt, completion_tokens=completion,
                   total_tokens=total)

    def __str__(self) -> str:
        if self.total_tokens is None:
            return "tokens: not reported"
        return (f"{self.total_tokens:,} tokens "
                f"({self.prompt_tokens if self.prompt_tokens is not None else '?'} in"
                f" + {self.completion_tokens if self.completion_tokens is not None else '?'} out)")


@dataclass(frozen=True)
class CallRecord:
    """One model call, with enough context to explain a surprising bill."""
    task: str
    model: str
    usage: TokenUsage
    stubbed: bool = False
    failed: bool = False
    cost_usd: Optional[float] = None


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

@dataclass
class UsageLedger:
    """
    Accumulates call records for a run.

    Thread-safe because ingestion may fan out across files; an undercount
    caused by a race would be worse than useless, since it would look like
    a real (low) number rather than an obvious error.
    """
    records: List[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, record: CallRecord) -> None:
        with self._lock:
            self.records.append(record)

    def reset(self) -> None:
        """Clear the ledger — used between runs and by tests."""
        with self._lock:
            self.records.clear()

    # -- aggregates -------------------------------------------------------

    @property
    def live_records(self) -> List[CallRecord]:
        """Calls that actually reached a provider. Stubs cost nothing."""
        return [r for r in self.records if not r.stubbed]

    @property
    def total_tokens(self) -> int:
        return sum(r.usage.total_tokens or 0 for r in self.live_records)

    @property
    def total_cost_usd(self) -> Optional[float]:
        """
        Total spend, or None if NOTHING could be priced.

        When only some calls have known rates the total is the sum of those,
        and summary() says how many calls are missing from it — a partial
        total labelled as partial, never a partial total passed off as
        complete.
        """
        known = [r.cost_usd for r in self.live_records if r.cost_usd is not None]
        return sum(known) if known else None

    @property
    def unpriced_call_count(self) -> int:
        return len([r for r in self.live_records if r.cost_usd is None])

    def by_task(self) -> Dict[str, Dict[str, Any]]:
        """Tokens and cost grouped by task — the 'what was expensive' view."""
        grouped: Dict[str, Dict[str, Any]] = {}
        for r in self.live_records:
            entry = grouped.setdefault(
                r.task, {"calls": 0, "tokens": 0, "cost_usd": 0.0,
                         "cost_known": True})
            entry["calls"] += 1
            entry["tokens"] += r.usage.total_tokens or 0
            if r.cost_usd is None:
                entry["cost_known"] = False
            else:
                entry["cost_usd"] += r.cost_usd
        return grouped

    def as_dict(self) -> Dict[str, Any]:
        """Machine-readable form, for embedding in a run report."""
        return {
            "calls_total": len(self.records),
            "calls_live": len(self.live_records),
            "calls_stubbed": len([r for r in self.records if r.stubbed]),
            "calls_failed": len([r for r in self.records if r.failed]),
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "unpriced_calls": self.unpriced_call_count,
            "by_task": self.by_task(),
        }

    def summary(self) -> str:
        """Human-readable block, safe to print at the end of any run."""
        if not self.records:
            return "AI usage: no model calls were made."

        stubbed = len([r for r in self.records if r.stubbed])
        failed = len([r for r in self.records if r.failed])
        live = len(self.live_records)

        lines = ["AI usage", "--------",
                 f"  calls          : {len(self.records)} "
                 f"({live} live, {stubbed} stubbed)"]
        if failed:
            lines.append(
                f"  FAILED calls   : {failed}  <-- these may still have been "
                f"billed by the provider")
        lines.append(f"  tokens (live)  : {self.total_tokens:,}")

        cost = self.total_cost_usd
        if cost is None:
            lines.append("  estimated cost : unknown — no rate configured for "
                         "the model(s) used")
        else:
            note = ""
            if self.unpriced_call_count:
                note = (f"  (PARTIAL — excludes {self.unpriced_call_count} "
                        f"call(s) with no configured rate)")
            lines.append(f"  estimated cost : ~${cost:.4f}{note}")
        lines.append("  (estimate only — the provider's billing is the "
                     "authority)")

        by_task = self.by_task()
        if len(by_task) > 1:
            lines.append("  by task:")
            for task, entry in sorted(by_task.items(),
                                      key=lambda kv: -kv[1]["tokens"]):
                cost_text = (f"~${entry['cost_usd']:.4f}"
                             if entry["cost_known"] else "cost unknown")
                lines.append(f"    - {task}: {entry['calls']} call(s), "
                             f"{entry['tokens']:,} tokens, {cost_text}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level default ledger
# ---------------------------------------------------------------------------
# A process-wide default so a new call site needs no plumbing to be counted.
# Anything wanting isolation (a test, a parallel run) builds its own
# UsageLedger() and passes it explicitly.
_LEDGER = UsageLedger()


def ledger() -> UsageLedger:
    """The process-wide ledger."""
    return _LEDGER


def record_call(*, task: str, model: str,
                usage: Optional[Mapping[str, Any]] = None,
                stubbed: bool = False, failed: bool = False,
                into: Optional[UsageLedger] = None) -> Optional[CallRecord]:
    """
    Record one model call. THE function every call site uses.

    Never raises. Accounting must not be able to break the work it measures,
    so a malformed usage payload is dropped quietly and the run continues.
    """
    try:
        counts = TokenUsage.from_mapping(usage)
        cost = None if stubbed else estimate_cost_usd(
            model, counts.prompt_tokens, counts.completion_tokens)
        record = CallRecord(task=task, model=model, usage=counts,
                            stubbed=stubbed, failed=failed, cost_usd=cost)
        (into or _LEDGER).record(record)
        return record
    except Exception:
        return None
