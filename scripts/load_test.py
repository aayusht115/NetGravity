#!/usr/bin/env python
"""
Measure what this application actually does under concurrent load.

    python scripts/load_test.py --base http://127.0.0.1:5050 \
        --email you@example.com --password ... --duration 60 --clients 8

Why this exists
---------------
`docs/operations.md` states rate limits and a gunicorn worker timeout, and the
previous report was explicit that both were "reasoned, not measured". A reasoned
timeout is a guess with a justification attached. This produces the measurement:
real HTTP requests against a running instance, at a stated concurrency, for a
stated duration, reporting latency percentiles and error rates per endpoint.

What it measures, and why those endpoints
-----------------------------------------
Three shapes of request, because they fail differently:

* **cheap reads** (`/api/status`, `/api/projects`) — the floor. If these are
  slow under load the problem is the server, not the solver.
* **analysis reads** (`/api/kpis/network`, `/api/insights`) — served from the
  stored analysis after the first solve. This is what a dashboard load costs
  once a network has been analysed, and it is the number that decides whether
  the platform feels responsive.
* **the first solve** — measured once, separately, and NOT included in the
  percentiles. Mixing a 30-second MILP into a distribution of 20-millisecond
  cache reads produces a p99 that describes neither.

What it does NOT do
-------------------
It does not tell you how many users the platform supports. That depends on the
size of their networks, which varies by two orders of magnitude between the demo
fixture and a real client's footprint. What it gives you is a measurement on THE
network you point it at, which is the only kind that means anything.

It also does not measure the solver's scaling. `--profile-solve` reports the
first solve's wall time for the bound network; a solver scaling study is a
different exercise against networks of graded size.

Safety
------
Read-only. Every endpoint it calls is a GET. It authenticates as a real user and
respects rate limits — a 429 is RECORDED, not retried, because the point is to
find out where the limit sits.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@dataclass
class Samples:
    """Latency samples and outcomes for one endpoint."""
    name: str
    latencies_ms: List[float] = field(default_factory=list)
    statuses: Dict[int, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def record(self, ms: float, status: int) -> None:
        self.latencies_ms.append(ms)
        self.statuses[status] = self.statuses.get(status, 0) + 1

    @property
    def n(self) -> int:
        return len(self.latencies_ms)

    @property
    def ok(self) -> int:
        return sum(c for s, c in self.statuses.items() if 200 <= s < 300)

    @property
    def limited(self) -> int:
        return self.statuses.get(429, 0)

    @property
    def refused(self) -> int:
        """Connections the server never completed. A different fault from a 5xx."""
        return self.statuses.get(0, 0)

    @property
    def server_error(self) -> int:
        return sum(c for s, c in self.statuses.items() if s >= 500)

    @property
    def failed(self) -> int:
        return self.refused + self.server_error

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        # Nearest-rank. With a few hundred samples an interpolated percentile
        # invents precision the sample size does not support.
        index = min(len(ordered) - 1, max(0, int(round(p / 100.0 * len(ordered))) - 1))
        return ordered[index]

    def summary(self) -> Dict[str, object]:
        return {
            "endpoint": self.name,
            "requests": self.n,
            "ok": self.ok,
            "rate_limited_429": self.limited,
            "connections_refused": self.refused,
            "server_errors_5xx": self.server_error,
            "statuses": {str(k): v for k, v in sorted(self.statuses.items())},
            "p50_ms": round(self.percentile(50), 1),
            "p90_ms": round(self.percentile(90), 1),
            "p99_ms": round(self.percentile(99), 1),
            "max_ms": round(max(self.latencies_ms), 1) if self.latencies_ms else 0.0,
            "mean_ms": round(statistics.fmean(self.latencies_ms), 1)
                       if self.latencies_ms else 0.0,
        }


class Session:
    """One authenticated HTTP session, cookie-based like the browser's."""

    def __init__(self, base: str) -> None:
        import requests
        self.base = base.rstrip("/")
        self.http = requests.Session()

    def login(self, email: str, password: str) -> Tuple[bool, str]:
        # The landing page sets the CSRF cookie; unsafe methods need it echoed.
        self.http.get(f"{self.base}/", timeout=30)
        csrf = self.http.cookies.get("ng_csrf", "")
        r = self.http.post(
            f"{self.base}/api/auth/login",
            json={"email": email, "password": password},
            headers={"X-CSRF-Token": csrf}, timeout=30)
        if r.status_code != 200:
            return False, f"login returned {r.status_code}: {r.text[:200]}"
        return True, ""

    def get(self, path: str, timeout: float = 120.0) -> Tuple[float, int]:
        """`(latency_ms, status)`. A transport failure is status 0, never a raise."""
        started = time.perf_counter()
        try:
            r = self.http.get(f"{self.base}{path}", timeout=timeout)
            status = r.status_code
        except Exception:  # noqa: BLE001 — a timeout is a measurement
            status = 0
        return (time.perf_counter() - started) * 1000.0, status

    def first_project(self) -> Optional[str]:
        r = self.http.get(f"{self.base}/api/projects", timeout=30)
        if r.status_code != 200:
            return None
        projects = (r.json() or {}).get("projects") or []
        return projects[0]["id"] if projects else None


def profile_first_solve(session: Session, project_id: str) -> Dict[str, object]:
    """
    What the analysis of this network cost, and whether this call paid for it.

    Measured on its own and excluded from the percentiles: a 30-second MILP
    inside a distribution of cache reads produces a p99 that describes neither
    of them.

    The response carries `compute_seconds` — how long the solve took when it
    actually ran — so a request served from the store still reports the real
    cost of the analysis rather than the cost of fetching it. Without that, a
    warm store made this print "first analysis: 31 ms" and call it a solve.
    """
    import requests
    started = time.perf_counter()
    try:
        r = session.http.get(
            f"{session.base}/api/kpis/network?project_id={project_id}", timeout=600.0)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        body = r.json() if r.status_code == 200 else {}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "error": str(exc)[:200]}

    solve_seconds = body.get("compute_seconds")
    served_from_store = bool(
        solve_seconds is not None and elapsed_ms < solve_seconds * 1000.0 * 0.5)
    return {
        "request_ms": round(elapsed_ms, 1),
        "solve_seconds": solve_seconds,
        "served_from_store": served_from_store,
        "status": r.status_code,
    }


def run_load(base: str, email: str, password: str, project_id: str,
             clients: int, duration: float,
             think_seconds: float = 0.0) -> Dict[str, Samples]:
    endpoints = [
        ("status", "/api/status"),
        ("projects", "/api/projects"),
        ("kpis.network", f"/api/kpis/network?project_id={project_id}"),
        ("kpis.facilities", f"/api/kpis/facilities?project_id={project_id}"),
        ("insights", f"/api/insights?project_id={project_id}&scope=NETWORK"),
    ]
    samples = {name: Samples(name) for name, _ in endpoints}
    lock = threading.Lock()
    deadline = time.time() + duration
    failures: List[str] = []

    # Each worker holds its OWN session. Sharing one `requests.Session` across
    # threads shares a connection pool, which serialises exactly the
    # concurrency being measured.
    def worker(index: int) -> None:
        session = Session(base)
        ok, reason = session.login(email, password)
        if not ok:
            with lock:
                failures.append(f"client {index}: {reason}")
            return
        i = 0
        while time.time() < deadline:
            name, path = endpoints[i % len(endpoints)]
            i += 1
            ms, status = session.get(path)
            with lock:
                samples[name].record(ms, status)
            if think_seconds > 0:
                time.sleep(think_seconds)

    threads = [threading.Thread(target=worker, args=(n,), daemon=True)
               for n in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration + 120)

    if failures:
        for line in failures:
            print(f"  WARNING {line}")
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:5050")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--out", default="",
                        help="write the measurement to this JSON file")
    parser.add_argument("--profile-solve", action="store_true",
                        help="measure the analysis before loading")
    parser.add_argument("--think", type=float, default=0.0,
                        help="seconds a client pauses between requests. 0 is a "
                             "flood, which finds the ceiling; a second or two "
                             "resembles people using dashboards, which is what "
                             "a latency target should be set against.")
    args = parser.parse_args()

    try:
        import requests  # noqa: F401
    except ImportError:
        raise SystemExit("This script needs `requests`: pip install requests")

    primary = Session(args.base)
    ok, reason = primary.login(args.email, args.password)
    if not ok:
        raise SystemExit(reason)

    project_id = args.project_id or primary.first_project()
    if not project_id:
        raise SystemExit("No project to measure. Pass --project-id.")

    print(f"target      : {args.base}")
    print(f"project     : {project_id}")
    print(f"clients     : {args.clients}")
    print(f"duration    : {args.duration:.0f}s")
    print()

    solve: Dict[str, object] = {}
    if args.profile_solve:
        print("measuring the analysis (runs the MILP if it is not stored)...")
        solve = profile_first_solve(primary, project_id)
        seconds = solve.get("solve_seconds")
        if solve.get("served_from_store"):
            print(f"  request      : {solve['request_ms']:.0f} ms "
                  f"(SERVED FROM THE STORE, not a solve)")
            print(f"  the solve took {seconds:.1f} s when it ran"
                  if isinstance(seconds, (int, float))
                  else "  the stored analysis does not record its solve time")
        elif isinstance(seconds, (int, float)):
            print(f"  solve        : {seconds:.1f} s "
                  f"(request {solve['request_ms']:.0f} ms)")
        else:
            print(f"  request      : {solve.get('request_ms', 0):.0f} ms "
                  f"(status {solve.get('status')})")
        print()
    else:
        # Warm the analysis anyway, or the first request of the run pays for the
        # solve and lands in the percentiles as an outlier that is not a
        # property of the served path.
        print("warming the stored analysis...")
        primary.get(f"/api/kpis/network?project_id={project_id}", timeout=600.0)
        primary.get(f"/api/insights?project_id={project_id}&scope=NETWORK",
                    timeout=600.0)

    print("loading...")
    started = time.time()
    samples = run_load(args.base, args.email, args.password, project_id,
                       args.clients, args.duration, args.think)
    elapsed = time.time() - started

    total = sum(s.n for s in samples.values())
    print()
    print(f"{'endpoint':22} {'reqs':>6} {'ok':>6} {'429':>6} {'5xx':>5} "
          f"{'refused':>8} {'p50':>7} {'p90':>7} {'p99':>7} {'max':>8}")
    print("-" * 98)
    rows = []
    for name in ("status", "projects", "kpis.network", "kpis.facilities", "insights"):
        s = samples[name]
        row = s.summary()
        rows.append(row)
        print(f"{name:22} {s.n:>6} {s.ok:>6} {s.limited:>6} {s.server_error:>5} "
              f"{s.refused:>8} {row['p50_ms']:>7.1f} {row['p90_ms']:>7.1f} "
              f"{row['p99_ms']:>7.1f} {row['max_ms']:>8.1f}")

    throughput = total / elapsed if elapsed else 0.0
    refused = sum(s.refused for s in samples.values())
    server_errors = sum(s.server_error for s in samples.values())
    failed = refused + server_errors
    limited = sum(s.limited for s in samples.values())

    print("-" * 98)
    print(f"{'TOTAL':22} {total:>6} "
          f"{sum(s.ok for s in samples.values()):>6} {limited:>6} "
          f"{server_errors:>5} {refused:>8}")
    print()
    print(f"throughput  : {throughput:.1f} requests/second sustained")
    print(f"elapsed     : {elapsed:.1f}s")

    # What the measurement says about the configuration.
    print()
    print("Against the configured limits")
    print("-" * 90)
    worst_p99 = max((r["p99_ms"] for r in rows), default=0.0)
    print(f"  worst p99 across endpoints : {worst_p99:.0f} ms")
    solve_seconds = solve.get("solve_seconds") if solve else None
    if isinstance(solve_seconds, (int, float)):
        print(f"  the analysis solve took    : {solve_seconds:.1f} s")
        print("  gunicorn --timeout must exceed this by a margin; "
              "docs/operations.md states 300 s.")
        if solve_seconds > 240:
            print("  WARNING: the solve is within 60 s of the documented "
                  "300 s worker timeout on THIS network.")
    if limited:
        print(f"  429s returned              : {limited} — the rate limit was "
              f"reached at this concurrency, which is the limit working.")
    else:
        print("  429s returned              : 0 — this load stayed inside the "
              "configured limits.")
    if server_errors:
        print(f"  SERVER ERRORS              : {server_errors} requests returned "
              f"5xx. Investigate before drawing any conclusion from the "
              f"latencies above — the application faulted under this load.")
    if refused:
        print(f"  connections refused        : {refused} of {total} "
              f"({refused / total * 100:.0f}%) never completed. This is the "
              f"server refusing work it has no capacity to accept, not the "
              f"application faulting: the requests that DID complete are still "
              f"a valid sample, and the refusal rate is the capacity finding.")
        print(f"  completed rate             : "
              f"{(total - refused) / elapsed:.0f} requests/second")

    server = ""
    try:
        import requests
        server = requests.get(f"{args.base}/api/status", timeout=10).headers.get(
            "Server", "")
    except Exception:  # noqa: BLE001
        pass
    print()
    print(f"served by   : {server or 'unknown'}")
    if "Werkzeug" in server:
        print("  NOTE: this is Flask's DEVELOPMENT server. It is not what "
              "production runs, and its concurrency behaviour is not "
              "representative — the latencies above are a floor for a "
              "gunicorn deployment, not a prediction of it.")

    measurement = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "served_by": server,
        "base": args.base,
        "project_id": project_id,
        "clients": args.clients,
        "think_seconds": args.think,
        "duration_seconds": round(elapsed, 1),
        "throughput_rps": round(throughput, 2),
        "endpoints": rows,
        "first_solve": solve,
        "totals": {"requests": total, "rate_limited": limited,
                   "connections_refused": refused,
                   "server_errors_5xx": server_errors},
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(measurement, fh, indent=2)
        print(f"\nwrote {args.out}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
