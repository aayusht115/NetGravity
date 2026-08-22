#!/usr/bin/env python3
"""
FLOW 1 — Configuration and provider reachability
=================================================
Costs NOTHING. Run this first, every time.

    python scripts/verify_1_config.py

Answers three questions before any budget is spent:
  - which provider did the config actually resolve to (not which one you
    meant to configure)
  - is the endpoint reachable at all
  - how much of the shared budget is left

The gateway's budget is shared with everyone holding the same token and is
cumulative — it does not reset daily — so knowing what is left before a
batch run is worth the ten seconds.
"""

from __future__ import annotations

import argparse
import json
import urllib.request

from _verify_common import add_common_flags, finish, section, start


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_flags(parser)
    parser.parse_args()

    config = start("FLOW 1 — CONFIG AND PROVIDER", needs_ai=False)

    section("resolved settings")
    print(f"  provider          : {config.llm_provider}")
    print(f"  model             : {config.resolved_model}")
    print(f"  stub mode         : {config.stub_mode}")
    print(f"  key source        : {config.key_source}")
    print(f"  timeout / retries : {config.llm_timeout_seconds}s / "
          f"{config.llm_max_retries}")
    print(f"  strict mode       : {config.llm_strict}")

    provider = (config.llm_provider or "").lower()
    if provider == "gateway":
        print(f"  gateway url       : {config.gateway_url}")
    elif config.llm_base_url:
        print(f"  base url          : {config.llm_base_url}")

    section("reachability")
    if provider == "gateway":
        base = (config.gateway_url or "").rstrip("/")
        if not base:
            print("  NETGRAVITY_GATEWAY_URL is not set — nothing to check.")
            return 1
        # /health needs no auth and costs no budget or request quota.
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=15) as resp:
                print(f"  health            : {json.loads(resp.read().decode())}")
        except Exception as exc:
            print(f"  health            : UNREACHABLE — {exc}")
            return 1
    else:
        print("  (health check is gateway-only; other providers have no "
              "unauthenticated endpoint to probe for free)")

    print("\n  No model call was made. Nothing was spent.")
    return finish(config)


if __name__ == "__main__":
    raise SystemExit(main())
