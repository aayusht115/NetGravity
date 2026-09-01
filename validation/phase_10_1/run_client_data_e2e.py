"""Ingest the client's sample workbook and check every module against it.

Uses `Dump/NetGravity_Test_Data_Clean.xlsx` — a normalised, multi-sheet
workbook of the kind a real client sends: facilities, markets, lanes, products,
36 months of demand history, monthly capacity, a freight-rate table keyed by
lane and product, and external signals.

This drives the real HTTP API end to end and asserts on what comes back, so a
regression in parsing, assembly, solving, KPI reporting or forecasting shows up
as a failed check rather than a blank screen.
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import uuid
from wsgiref.simple_server import make_server

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

WORKBOOK = ROOT.parent / "Dump" / "NetGravity_Test_Data_Clean.xlsx"
OUT = pathlib.Path(__file__).parent
PORT = 5107
BASE = f"http://127.0.0.1:{PORT}"

results = {"checks": []}


def record(cid: str, name: str, status: str, detail: str = "") -> None:
    results["checks"].append({"id": cid, "name": name, "status": status, "detail": detail})
    print(f"[{status:5}] {cid}  {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    import requests
    from app.backend.app import app

    # Rate-limit counters are now SHARED and durable, so they no longer reset
    # when a process does. That is correct for a rate limit and wrong for a
    # harness: a sequence of runs from one address exhausts one signup budget
    # between them, and the next run fails at "Account created" with a 429 that
    # is about the previous run rather than about the code under test.
    #
    # The window is cleared, NOT the limiter disabled — the control stays in the
    # path, and `netgravity/tests/integration/test_operational_hardening.py`
    # is where its behaviour is actually asserted.
    try:
        from app.backend.services.ratelimit import limiter as _limiter
        _limiter.reset()
    except Exception:
        pass


    if not WORKBOOK.exists():
        print(f"workbook not found: {WORKBOOK}")
        return 2

    server = make_server("127.0.0.1", PORT, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(1.0)

    s = requests.Session()
    email = f"client-{uuid.uuid4().hex[:8]}@example.com"

    r = s.post(f"{BASE}/api/auth/signup", json={
        "name": "Client Data Test", "email": email, "password": "Netgravity@2026"})
    record("C-01", "Account created", "PASS" if r.status_code == 201 else "FAIL",
           f"HTTP {r.status_code}")
    token = (r.json().get("token") or "") if r.ok else ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    r = s.post(f"{BASE}/api/projects", json={"name": "Client Sample Network"},
               headers=headers)
    project_id = r.json().get("project", {}).get("id") or r.json().get("id")
    record("C-02", "Project created", "PASS" if r.status_code == 201 else "FAIL",
           f"project_id={project_id}")

    # ---- 1. Parse the workbook ---------------------------------------
    with open(WORKBOOK, "rb") as fh:
        r = s.post(f"{BASE}/api/ingestions/preview/upload-and-parse",
                   params={"project_id": project_id},
                   files={"files": (WORKBOOK.name, fh,
                                    "application/vnd.openxmlformats-officedocument."
                                    "spreadsheetml.sheet")},
                   headers=headers)
    ok = r.status_code == 200
    preview = r.json() if ok else {}
    structure = preview.get("structure") or {}
    record("C-03", "Workbook parsed", "PASS" if ok else "FAIL",
           f"HTTP {r.status_code}, sheets={len(preview.get('files', [{}])[0].get('sheets', []))}")

    counts = {k: len(structure.get(k) or []) for k in
              ("plants", "dcs", "markets", "lanes", "products",
               "demandHistory", "capacityHistory", "signals")}
    # The workbook holds 3 plants, 5 DCs, 7 markets, 36 lanes, 2 products,
    # 504 demand observations, 288 capacity rows and 10 signals.
    expected = {"plants": 3, "dcs": 5, "markets": 7, "lanes": 36, "products": 2,
                "demandHistory": 504, "capacityHistory": 288, "signals": 10}
    wrong = {k: (counts[k], v) for k, v in expected.items() if counts[k] != v}
    record("C-04", "Every sheet read, each exactly once",
           "PASS" if not wrong else "FAIL",
           json.dumps(counts) if not wrong else f"got/want {wrong}")

    caps = sorted({(p["id"], p["capacity"]) for p in structure.get("plants") or []})
    record("C-05", "Facility capacities are the uploaded values",
           "PASS" if caps and all(c not in (None, 10000.0) for _, c in caps) else "FAIL",
           str(caps))

    demands = {m["id"]: m.get("demand") for m in structure.get("markets") or []}
    record("C-06", "Market demand derived from the demand history",
           "PASS" if demands and all(v for v in demands.values()) else "FAIL",
           json.dumps(demands))

    exact = [m["id"] for m in (structure.get("markets") or []) if m.get("coordsExact")]
    record("C-07", "Market coordinates come from the file, not a hash",
           "PASS" if len(exact) == 7 else "FAIL", f"exact for {len(exact)}/7")

    rates = [l for l in (structure.get("lanes") or []) if l.get("ratesByProduct")]
    record("C-08", "Freight rates joined from the rates table",
           "PASS" if len(rates) == 36 else "FAIL", f"{len(rates)}/36 lanes priced")

    leads = {l.get("leadTime") for l in structure.get("lanes") or []}
    record("C-09", "Transit times are the uploaded values",
           "PASS" if len(leads) > 1 else "FAIL",
           f"{len(leads)} distinct transit times")

    # ---- 2. Commit → assemble → bind ---------------------------------
    r = s.post(f"{BASE}/api/ingestions/preview/commit",
               json={"project_id": project_id}, headers=headers)
    commit = r.json()
    record("C-10", "Network assembled and bound",
           "PASS" if r.status_code == 201 else "FAIL",
           json.dumps(commit.get("network_summary") or commit.get("error") or {})[:200])

    summary = commit.get("network_summary") or {}
    record("C-11", "Product dimension preserved",
           "PASS" if summary.get("products") == 2 else "FAIL",
           f"products={summary.get('products')}")
    record("C-12", "Demand history carried to the forecaster",
           "PASS" if summary.get("demand_history_series") == 14 else "FAIL",
           f"series={summary.get('demand_history_series')}")

    issues = commit.get("issues") or []
    named = [i for i in issues if "M001" in i or "M005" in i or "M002" in i]
    record("C-13", "Unservable markets named before the solve runs",
           "PASS" if len(named) >= 3 else "FAIL",
           f"{len(issues)} issue(s); {len(named)} name a market")

    # ---- 3. KPIs ------------------------------------------------------
    r = s.get(f"{BASE}/api/kpis/network", params={"project_id": project_id},
              headers=headers)
    kpis = r.json() if r.ok else {}
    record("C-14", "KPI endpoint answers for the uploaded network",
           "PASS" if r.status_code == 200 else "FAIL", f"HTTP {r.status_code}")

    payload = kpis.get("kpis") or {}
    statuses = {k: (v or {}).get("status") for k, v in payload.items()
                if isinstance(v, dict)}
    infeasible = [k for k, v in statuses.items() if v == "INFEASIBLE"]
    valid = [k for k, v in statuses.items() if v == "VALID"]
    record("C-15", "Every network KPI carries a value",
           "PASS" if valid and not infeasible else "FAIL",
           f"{len(valid)} VALID, {len(infeasible)} INFEASIBLE of {len(statuses)}")

    diag = kpis.get("diagnosis") or commit.get("issues") or []
    record("C-16", "A reason accompanies an unservable network",
           "PASS" if diag else "FAIL", str(diag)[:160])

    # The strict model is still infeasible on this workbook. Every figure above
    # therefore describes a plan that leaves demand stranded, and must say so.
    relaxed = [k for k, v in payload.items()
               if isinstance(v, dict)
               and (v.get("metadata") or {}).get("solve_relaxation")]
    record("C-15b", "Every figure declares that it came from a relaxed solve",
           "PASS" if len(relaxed) == len(statuses) else "FAIL",
           f"{len(relaxed)} of {len(statuses)} carry solve_relaxation")

    def kval(metric):
        entry = payload.get(metric) or {}
        return entry.get("value") if entry.get("status") == "VALID" else None

    # The stranded demand the servability diagnosis predicted, confirmed by the
    # solver rather than asserted alongside it.
    unserved, total = kval("unserved_demand"), kval("total_demand")
    record("C-15c", "The solver's shortfall matches the pre-flight diagnosis",
           "PASS" if unserved == 8733 and total == 36982 else "FAIL",
           f"{unserved} unserved of {total}")

    # The cost breakdown behind the total. These were computed on every solve
    # and never exposed, so the dashboard's breakdown had nothing to read.
    parts = {m: kval(m) for m in ("facility_cost", "transport_cost",
                                  "handling_cost", "inventory_cost",
                                  "carbon_cost", "opening_cost", "closure_cost")}
    total_cost = kval("business_network_cost")
    reconciles = (all(v is not None for v in parts.values())
                  and total_cost is not None
                  and abs(sum(parts.values()) - total_cost) < 0.01)
    record("C-15d", "Cost components are exposed and reconcile with the total",
           "PASS" if reconciles else "FAIL",
           f"sum={sum(v for v in parts.values() if v is not None):,.2f} "
           f"vs total={total_cost}")

    # The shortage penalty is a solver device, not a price. It must not be
    # inside the business cost.
    penalty = kval("shortage_penalty_cost")
    record("C-15e", "The notional shortage penalty is excluded from business cost",
           "PASS" if penalty and total_cost and penalty > total_cost else "FAIL",
           f"penalty={penalty:,.0f} business={total_cost:,.2f}"
           if penalty and total_cost else "missing")

    notional = ((payload.get("shortage_penalty_cost") or {}).get("metadata")
                or {}).get("notional")
    record("C-15f", "The shortage penalty is labelled notional",
           "PASS" if notional else "FAIL", str(notional)[:100])

    # ---- 4. Topology ---------------------------------------------------
    # Structure is INPUT: it exists whether or not a solve succeeds, so the
    # Digital Twin must render the network even when the answer is infeasible.
    r = s.get(f"{BASE}/api/network/structure", params={"project_id": project_id},
              headers=headers)
    struct = r.json() if r.ok else {}
    nodes = (len(struct.get("plants") or []) + len(struct.get("dcs") or [])
             + len(struct.get("markets") or []))
    record("C-17", "Digital Twin sees the whole network despite an infeasible solve",
           "PASS" if nodes == 15 and len(struct.get("lanes") or []) == 36 else "FAIL",
           f"{nodes} nodes (3 plants + 5 DCs + 7 markets), "
           f"{len(struct.get('lanes') or [])} lanes")

    mkt_demand = {m["id"]: m.get("demand") for m in struct.get("markets") or []}
    record("C-17b", "Markets carry their demand and SLA",
           "PASS" if mkt_demand and all(mkt_demand.values()) else "FAIL",
           json.dumps(mkt_demand))

    # C-17c used to assert the opposite: that facility KPIs stayed EMPTY,
    # because the strict solve produced no flows and inventing a utilisation
    # would have been a fabrication. That premise no longer holds — the engine
    # now returns a relaxed plan, which has real flows — so the check is not
    # dropped but tightened: the utilisation must be present, must belong to
    # every facility, and must be a percentage of the capacity the upload
    # stated.
    r = s.get(f"{BASE}/api/kpis/facilities", params={"project_id": project_id},
              headers=headers)
    facs = (r.json() or {}).get("facilities") or {}
    consistent = []
    for fid, metrics in facs.items():
        util = (metrics.get("utilization_pct") or {}).get("value")
        thru = (metrics.get("throughput_units") or {}).get("value")
        cap = (metrics.get("capacity_units") or {}).get("value")
        if util is None or thru is None or not cap:
            continue
        consistent.append(abs(util - (thru / cap * 100.0)) < 0.05)
    record("C-17c", "Every facility reports a utilisation consistent with its capacity",
           "PASS" if len(facs) == 8 and consistent and all(consistent) else "FAIL",
           f"{len(facs)} facilities, {sum(bool(c) for c in consistent)} consistent")

    observed = {n["id"]: (n.get("observed") or {}).get("utilisationPct")
                for n in (struct.get("plants") or []) + (struct.get("dcs") or [])}
    record("C-17d", "The client's own recorded utilisation is carried through",
           "PASS" if len(observed) == 8 and all(v is not None for v in observed.values())
           else "FAIL",
           json.dumps(observed))

    # Lane volumes: solver output that had no HTTP surface, so every corridor
    # showed a null volume beside a freight rate the upload supplied.
    r = s.get(f"{BASE}/api/kpis/flows", params={"project_id": project_id},
              headers=headers)
    flows = (r.json() or {}).get("flows") or []
    flow_cost = sum(f.get("transport_cost") or 0.0 for f in flows)
    record("C-17e", "Solved lane volumes and costs are exposed",
           "PASS" if flows and abs(flow_cost - (kval("transport_cost") or -1)) < 0.01
           else "FAIL",
           f"{len(flows)} lanes, transport {flow_cost:,.2f} "
           f"vs network KPI {kval('transport_cost')}")

    # ---- 5. Forecast --------------------------------------------------
    r = s.get(f"{BASE}/api/forecast", params={"project_id": project_id, "horizon": 6},
              headers=headers)
    fc = r.json() if r.ok else {}
    series = fc.get("series") or fc.get("forecast") or []
    record("C-18", "Forecast runs on the uploaded history",
           "PASS" if r.status_code == 200 and series else "FAIL",
           f"HTTP {r.status_code}, {len(series)} series"
           + ("" if series else f" · {str(fc)[:180]}"))

    (OUT / "client_data_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")

    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    print(f"\n{passed} passed, {failed} failed of {len(results['checks'])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
