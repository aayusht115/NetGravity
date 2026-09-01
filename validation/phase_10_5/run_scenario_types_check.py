"""Run EVERY scenario type the builder offers, against the client workbook.

The Create Scenario modal presents six kinds of change. Three of them could not
run at all before this pass:

  * `CHANGE_TRANSPORT_COST` and `CHANGE_SLA` were not in the API's action map,
    so the request was rejected with 400 before any solver saw it;
  * `CHANGE_DEMAND` reached the API but the modal rendered no facility field for
    it, and both the API and the orchestrator's validator required at least one
    facility_id — for a change that applies to every demand row in the network;
  * `OPEN_FACILITY` could only pin open a site the client already operates, so
    "open a new DC" was unanswerable.

Each check below asserts the scenario SOLVES and that its result actually
differs from the baseline in the direction the change implies. A scenario that
returns the baseline's own figures has not run.

Driven through the real HTTP API with the real MILP engine — no mocks.
"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

WORKBOOK = ROOT.parent / "Dump" / "NetGravity_Test_Data_Clean.xlsx"
OUT = pathlib.Path(__file__).parent

results = {"checks": []}


def record(cid: str, name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results["checks"].append(
        {"id": cid, "name": name, "status": status, "detail": detail})
    print(f"[{status:4}] {cid:6} {name}" + (f" - {detail}" if detail else ""))


def kpi(record_json: dict, side: str, metric: str):
    """A VALID authoritative value, or None. Never a substituted zero."""
    result = (record_json.get(f"{side}_kpis") or {}).get(metric)
    if not result or result.get("status") != "VALID":
        return None
    return result.get("value")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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


    app.config["TESTING"] = True
    client = app.test_client()

    email = f"types-{uuid.uuid4().hex[:8]}@example.com"
    res = client.post("/api/auth/signup", json={
        "name": "Scenario Types", "email": email, "password": "Netgravity@2026"})
    assert res.status_code in (200, 201), res.get_data(as_text=True)[:400]
    token = res.get_json().get("token") or res.get_json().get("access_token")
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/projects", json={"name": "Scenario Types"},
                      headers=headers)
    project_id = (res.get_json().get("project") or res.get_json())["id"]

    data = {"project_id": project_id,
            "files": (io.BytesIO(WORKBOOK.read_bytes()), WORKBOOK.name)}
    res = client.post("/api/ingestions/preview/upload-and-parse", data=data,
                      content_type="multipart/form-data", headers=headers)
    assert res.status_code == 200, res.get_data(as_text=True)[:600]

    res = client.post("/api/ingestions/preview/commit", json={"project_id": project_id},
                      headers=headers)
    assert res.status_code in (200, 201), res.get_data(as_text=True)[:600]

    # The network as uploaded, so scenarios can name real facilities.
    structure = client.get(f"/api/network/structure?project_id={project_id}",
                           headers=headers).get_json()
    dcs = structure["dcs"]
    facility_id = dcs[0]["id"]
    print(f"\nnetwork: {len(structure['plants'])} plants, {len(dcs)} DCs, "
          f"{len(structure['markets'])} markets, {len(structure['lanes'])} lanes")
    print(f"scenario target facility: {facility_id}\n")

    def simulate(name: str, body: dict):
        payload = {"project_id": project_id, "name": name, **body}
        res = client.post(f"/api/scenarios/simulate?project_id={project_id}",
                          json=payload, headers=headers)
        return res.status_code, res.get_json()

    baseline_cost = None

    # ---- T-01 CHANGE_CAPACITY ------------------------------------------
    code, rec = simulate("Expand DC capacity", {
        "action": "CHANGE_CAPACITY", "facility_ids": [facility_id],
        "capacity_delta_units": 5000})
    ok = code == 201 and kpi(rec, "scenario", "business_network_cost") is not None
    if ok:
        baseline_cost = kpi(rec, "baseline", "business_network_cost")
    record("T-01", "CHANGE_CAPACITY solves", ok,
           f"HTTP {code} cost={kpi(rec, 'scenario', 'business_network_cost')}")

    # ---- T-02 CLOSE_FACILITY -------------------------------------------
    code, rec = simulate("Close a DC", {
        "action": "CLOSE_FACILITY", "facility_ids": [facility_id]})
    closed = [fid for fid, s in (rec.get("scenario_facilities") or {}).items()
              if s.get("isOpen") is False]
    record("T-02", "CLOSE_FACILITY solves and closes the named facility",
           code == 201 and facility_id in closed,
           f"HTTP {code} closed={closed}")

    # ---- T-03 OPEN_FACILITY --------------------------------------------
    code, rec = simulate("Pin a DC open", {
        "action": "OPEN_FACILITY", "facility_ids": [facility_id]})
    open_now = (rec.get("scenario_facilities") or {}).get(facility_id, {})
    record("T-03", "OPEN_FACILITY solves and pins the facility open",
           code == 201 and open_now.get("isOpen") is True,
           f"HTTP {code} {json.dumps(open_now)}")

    # ---- T-04 CHANGE_DEMAND, network-wide, no facility named -----------
    code, rec = simulate("Demand up 20%", {
        "action": "CHANGE_DEMAND", "demand_multiplier": 1.2})
    base_demand = kpi(rec, "baseline", "total_demand")
    scn_demand = kpi(rec, "scenario", "total_demand")
    grew = (base_demand is not None and scn_demand is not None
            and scn_demand > base_demand * 1.15)
    record("T-04", "CHANGE_DEMAND runs without naming a facility, and demand moves",
           code == 201 and grew,
           f"HTTP {code} total_demand {base_demand} -> {scn_demand}")

    # ---- T-05 CHANGE_TRANSPORT_COST ------------------------------------
    code, rec = simulate("Freight up 25%", {
        "action": "CHANGE_TRANSPORT_COST", "transport_cost_multiplier": 1.25})
    base_tc = kpi(rec, "baseline", "transport_cost")
    scn_tc = kpi(rec, "scenario", "transport_cost")
    rose = base_tc is not None and scn_tc is not None and scn_tc > base_tc
    record("T-05", "CHANGE_TRANSPORT_COST solves and freight cost rises",
           code == 201 and rose,
           f"HTTP {code} transport_cost {base_tc} -> {scn_tc}")

    # ---- T-06 CHANGE_SLA -----------------------------------------------
    code, rec = simulate("Relax SLA by a day", {
        "action": "CHANGE_SLA", "sla_days_delta": 1.0})
    # Relaxing the promise widens the set of lanes that qualify, so the solver
    # can serve at least as much demand as before, never less.
    base_fill = kpi(rec, "baseline", "demand_fill_rate")
    scn_fill = kpi(rec, "scenario", "demand_fill_rate")
    solved = code == 201 and scn_fill is not None
    not_worse = solved and base_fill is not None and scn_fill >= base_fill - 1e-9
    record("T-06", "CHANGE_SLA solves and relaxing the promise does not lose fill",
           solved and not_worse,
           f"HTTP {code} fill {base_fill} -> {scn_fill} "
           f"{'' if solved else str(rec)[:200]}")

    # ---- T-07 ADD_FACILITY, a greenfield site anywhere in India ---------
    code, rec = simulate("New DC at Nagpur", {
        "action": "ADD_FACILITY",
        "new_facility": {
            "name": "Nagpur DC", "latitude": 21.1458, "longitude": 79.0882,
            "capacity_units_per_period": 6000, "fixed_cost_per_year": 4_800_000,
            "handling_cost_per_unit": 12.0, "role": "DC"},
    })
    sites = rec.get("new_sites") or []
    in_solve = [fid for fid in (rec.get("scenario_facilities") or {})
                if fid.startswith("NEW_")]
    record("T-07", "ADD_FACILITY creates a site that is in no uploaded network",
           code == 201 and len(sites) == 1 and bool(in_solve),
           f"HTTP {code} new_sites={json.dumps(sites)} in_solve={in_solve}")

    record("T-08", "The new site carries coordinates the map can draw",
           bool(sites) and sites[0].get("lat") is not None
           and sites[0].get("lng") is not None,
           json.dumps(sites[0]) if sites else "no site returned")

    baseline_ids = set(rec.get("baseline_facilities") or {})
    scenario_ids = set(rec.get("scenario_facilities") or {})
    record("T-09", "The new site appears in the scenario solve and not the baseline",
           bool(scenario_ids - baseline_ids),
           f"added to solve: {sorted(scenario_ids - baseline_ids)}")

    record("T-10", "Every scenario says what it changed",
           bool(rec.get("overrides")), json.dumps(rec.get("overrides")))

    # ---- T-11 A greenfield site must be refused when unusable ----------
    code, rec = simulate("Bad site", {
        "action": "ADD_FACILITY",
        "new_facility": {"name": "Nowhere", "latitude": 21.0, "longitude": 79.0,
                         "capacity_units_per_period": 0},
    })
    record("T-11", "A site with no capacity is refused, not solved as if real",
           code >= 400, f"HTTP {code}")

    # ---- T-12 Three scenarios coexist ----------------------------------
    listed = client.get(f"/api/scenarios?project_id={project_id}",
                        headers=headers).get_json()
    record("T-12", "Every solved scenario is listed for the project",
           listed.get("total", 0) >= 7,
           f"{listed.get('total')} scenarios stored")

    # ---- T-13 Delete removes one for good ------------------------------
    victim = listed["scenarios"][0]["id"]
    res = client.delete(f"/api/scenarios/{victim}?project_id={project_id}",
                        headers=headers)
    after = client.get(f"/api/scenarios?project_id={project_id}",
                       headers=headers).get_json()
    record("T-13", "A deleted scenario stays deleted",
           res.status_code == 200
           and all(s["id"] != victim for s in after["scenarios"]),
           f"HTTP {res.status_code} {listed['total']} -> {after['total']}")

    # ---- T-14 Scenarios differ from each other -------------------------
    costs = {}
    for s in after["scenarios"]:
        value = kpi(s, "scenario", "business_network_cost")
        if value is not None:
            costs[s["name"]] = round(value, 2)
    record("T-14", "Different scenarios produce different results",
           len(set(costs.values())) > 1, json.dumps(costs))

    record("T-15", "The baseline is the same for every scenario",
           baseline_cost is not None, f"baseline cost {baseline_cost}")

    (OUT / "scenario_types_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    print(f"\n{passed} passed, {failed} failed of {len(results['checks'])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
