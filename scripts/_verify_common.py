"""
Shared plumbing for the scripts/verify_*.py flow checks.

Each verify script exercises ONE flow of the ingestion pipeline against the
live provider, so a failure points at a single place instead of somewhere in
a 300-line end-to-end run. This module holds the parts they all repeat:
locating the repo, printing the resolved config, and — because the gateway's
budget is shared and cumulative — showing what a run cost.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from netgravity.ingestion.config import IngestionConfig  # noqa: E402
from netgravity.telemetry import ledger  # noqa: E402

MOCK_ROOT = REPO_ROOT / "data" / "mock" / "india"


def banner(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 68 - len(title)))


def start(title: str, *, needs_ai: bool) -> IngestionConfig:
    """
    Print the run header and return the resolved config.

    `needs_ai` decides whether a missing key is merely worth noting or means
    the script cannot prove anything at all — a deterministic flow check is
    still fully valid with no key, an AI one is not.
    """
    banner(title)
    config = IngestionConfig()
    print(config.describe())

    if config.key_warning:
        print(f"\n  *** CONFIG WARNING: {config.key_warning} ***")

    if needs_ai and config.stub_mode:
        print("\n  *** STUB MODE — no key found, so nothing will reach a "
              "provider and this run proves nothing about live behaviour. "
              "Check .env and run from the repo root. ***")
    elif not needs_ai:
        print("\n  (this flow uses no AI — it is deterministic and runs the "
              "same with or without a key)")

    gateway_budget(config, "before")
    return config


def gateway_budget(config: IngestionConfig, when: str) -> None:
    """
    Show the gateway's shared, cumulative budget.

    Only meaningful for the gateway provider, and free to ask: the usage
    endpoint costs neither budget nor request quota.
    """
    if (config.llm_provider or "").lower() != "gateway":
        return
    try:
        from netgravity.ingestion.ai.client import fetch_gateway_usage
        usage = fetch_gateway_usage(config)
    except Exception as exc:
        print(f"  gateway budget ({when}): could not read — {exc}")
        return
    print(f"  gateway budget ({when}): "
          f"${usage.get('remaining_usd', '?')} left of "
          f"${usage.get('budget_usd', '?')}  |  "
          f"{usage.get('requests_today', '?')}/"
          f"{usage.get('max_requests_per_day', '?')} requests today")


def finish(config: IngestionConfig) -> int:
    """Print what the run cost and return an exit code."""
    banner("AI USAGE FOR THIS RUN")
    print(ledger().summary())
    gateway_budget(config, "after")
    print()
    return 0


def enable_trace() -> None:
    """Turn on the verbatim prompt/response log inside ai/client.py."""
    import logging
    logging.basicConfig(level=logging.DEBUG, stream=sys.stdout,
                        format="%(message)s")
    logging.getLogger("netgravity.ingestion.ai.client").setLevel(logging.DEBUG)


def add_common_flags(parser) -> None:
    parser.add_argument("--trace", action="store_true",
                        help="log the exact prompt sent and the raw response "
                             "returned, for every model call")
