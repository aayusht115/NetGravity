"""Prove the application survives a restart.

Every store in the application layer was an in-process dictionary: accounts,
sessions, projects, uploaded networks and solved scenarios all lived only in
the running process. Restarting the server returned a user to a sign-up form
with an afternoon's work gone. That single fact is why previous phases would
not call this production-ready.

This check does the only thing that settles it: it builds real state through
the HTTP API, throws the whole process state away, rebuilds the application
from scratch against the same database file, and asks for the same things back.
"""

from __future__ import annotations

import importlib
import io
import json
import pathlib
import sys
import tempfile
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
    sys.stdout.flush()


def build_app():
    """
    A COLD application, exactly as a restarted server builds one.

    Every module that holds state is dropped from `sys.modules` first, so the
    rebuilt application shares no dictionary, no singleton and no cached
    connection with the one before it. Reusing the imported module would prove
    nothing: the in-memory stores would still be populated and the test would
    pass on a system that loses everything.
    """
    for name in list(sys.modules):
        if name.startswith("app.backend") or name.startswith("netgravity.orchestrator"):
            del sys.modules[name]
    module = importlib.import_module("app.backend.app")
    return module


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    db_path = pathlib.Path(tempfile.mkdtemp(prefix="ng-persist-")) / "netgravity.db"
    import os
    os.environ["NETGRAVITY_DB_PATH"] = str(db_path)
    # The demo workspace is re-seeded on every start and is not persisted; it
    # would only add noise to the counts below.
    os.environ["NETGRAVITY_SEED_DEMO"] = "0"

    email = f"persist-{uuid.uuid4().hex[:8]}@example.com"
    password = "Netgravity@2026"
    name = "Ravi Deshmukh"

    # ================= FIRST RUN: create real state =====================
    mod = build_app()
    print(f"\n-- first start: {json.dumps(mod._DURABILITY_STATUS)}\n")
    client = mod.app.test_client()
    mod.app.config["TESTING"] = True

    res = client.post("/api/auth/signup", json={
        "name": name, "email": email, "password": password})
    assert res.status_code in (200, 201), res.get_data(as_text=True)[:300]
    token = res.get_json().get("token")
    headers = {"Authorization": f"Bearer {token}"}

    pj = client.post("/api/projects", json={"name": "Durable Network"},
                     headers=headers).get_json()
    project_id = (pj.get("project") or pj)["id"]

    client.post("/api/ingestions/preview/upload-and-parse",
                data={"project_id": project_id,
                      "files": (io.BytesIO(WORKBOOK.read_bytes()), WORKBOOK.name)},
                content_type="multipart/form-data", headers=headers)
    commit = client.post("/api/ingestions/preview/commit",
                         json={"project_id": project_id}, headers=headers)
    assert commit.status_code in (200, 201), commit.get_data(as_text=True)[:300]

    scn = client.post(f"/api/scenarios/simulate?project_id={project_id}",
                      json={"project_id": project_id, "name": "Close Kolkata DC",
                            "action": "CLOSE_FACILITY", "facility_ids": ["F006"]},
                      headers=headers).get_json()
    scenario_id = scn.get("id")
    scenario_cost = ((scn.get("scenario_kpis") or {})
                     .get("business_network_cost") or {}).get("value")

    before = {
        "kpis": client.get(f"/api/kpis/network?project_id={project_id}",
                           headers=headers).get_json(),
        "structure": client.get(f"/api/network/structure?project_id={project_id}",
                                headers=headers).get_json(),
        "forecast": client.get(f"/api/forecast?project_id={project_id}&horizon=6",
                               headers=headers).get_json(),
    }
    before_cost = ((before["kpis"].get("kpis") or {})
                   .get("business_network_cost") or {}).get("value")
    print(f"-- created: project={project_id} scenario={scenario_id} "
          f"cost={before_cost}\n")

    # ================= RESTART ==========================================
    del client, mod
    mod2 = build_app()
    status = mod2._DURABILITY_STATUS
    print(f"-- second start: {json.dumps(status)}\n")
    client2 = mod2.app.test_client()
    mod2.app.config["TESTING"] = True

    record("P-01", "The database reports what it restored",
           status.get("enabled") is True and status.get("users", 0) >= 1,
           json.dumps(status))

    # ---- P-02 The session still works ---------------------------------
    me = client2.get("/api/auth/me", headers=headers)
    record("P-02", "A session issued before the restart is still valid",
           me.status_code == 200
           and (me.get_json().get("user") or {}).get("email") == email,
           f"HTTP {me.status_code} {json.dumps(me.get_json())[:160]}")

    # ---- P-03 The account survives ------------------------------------
    login = client2.post("/api/auth/login", json={"email": email, "password": password})
    record("P-03", "The account and its password hash survive a restart",
           login.status_code == 200 and bool(login.get_json().get("token")),
           f"HTTP {login.status_code}")
    fresh = {"Authorization": f"Bearer {login.get_json().get('token')}"}

    wrong = client2.post("/api/auth/login",
                         json={"email": email, "password": "not-the-password"})
    record("P-04", "A restored account still rejects the wrong password",
           wrong.status_code in (401, 403), f"HTTP {wrong.status_code}")

    # ---- P-05 The project survives ------------------------------------
    projects = client2.get("/api/projects", headers=fresh).get_json()
    listed = [p for p in (projects.get("projects") or [])
              if p.get("id") == project_id]
    record("P-05", "The project survives, still bound to its network",
           bool(listed) and listed[0].get("has_network") is True,
           json.dumps(listed[0]) if listed else json.dumps(projects)[:200])

    # ---- P-06 The uploaded network survives ---------------------------
    structure = client2.get(f"/api/network/structure?project_id={project_id}",
                            headers=fresh)
    same_structure = (structure.status_code == 200
                      and structure.get_json().get("data_version")
                      == before["structure"].get("data_version"))
    record("P-06", "The uploaded network is still there, byte-for-byte the same",
           same_structure,
           f"HTTP {structure.status_code} version="
           f"{structure.get_json().get('data_version') if structure.status_code == 200 else '-'}")

    # ---- P-07 The solved KPIs still compute from it -------------------
    kpis = client2.get(f"/api/kpis/network?project_id={project_id}", headers=fresh)
    after_cost = ((kpis.get_json().get("kpis") or {})
                  .get("business_network_cost") or {}).get("value")
    record("P-07", "The restored network re-solves to the same cost",
           kpis.status_code == 200 and after_cost == before_cost,
           f"{before_cost} -> {after_cost}")

    # ---- P-08 Solved scenarios survive --------------------------------
    scenarios = client2.get(f"/api/scenarios?project_id={project_id}",
                            headers=fresh).get_json()
    restored_scn = next((s for s in (scenarios.get("scenarios") or [])
                         if s.get("id") == scenario_id), None)
    record("P-08", "A solved scenario survives with its figures intact",
           restored_scn is not None
           and ((restored_scn.get("scenario_kpis") or {})
                .get("business_network_cost") or {}).get("value") == scenario_cost,
           f"{scenarios.get('total')} scenarios, cost={scenario_cost}")

    # ---- P-09 The scenario can still explain itself --------------------
    # `new_sites` and `overrides` are read from the MATERIALISED scenario
    # network held by the orchestrator, not from the stored record — so this
    # fails unless that network was persisted too.
    record("P-09", "A restored scenario still knows what it changed",
           bool(restored_scn and restored_scn.get("overrides")),
           json.dumps(restored_scn.get("overrides")) if restored_scn else "")

    # ---- P-10 Uploaded history survives -------------------------------
    forecast = client2.get(f"/api/forecast?project_id={project_id}&horizon=6",
                           headers=fresh).get_json()
    before_series = len((before["forecast"] or {}).get("series") or [])
    after_series = len((forecast or {}).get("series") or [])
    record("P-10", "The demand history that arrived with the upload survives",
           after_series > 0 and after_series == before_series,
           f"{before_series} series -> {after_series}")

    # ---- P-11 Recorded capacity history survives ----------------------
    observed = [d for d in (structure.get_json().get("dcs") or [])
                if d.get("observed")]
    record("P-11", "The client's recorded capacity history survives",
           bool(observed),
           f"{len(observed)} facilities carry a recorded prior")

    # ---- P-12 A new scenario appends rather than replacing -------------
    client2.post(f"/api/scenarios/simulate?project_id={project_id}",
                 json={"project_id": project_id, "name": "After restart",
                       "action": "CHANGE_DEMAND", "demand_multiplier": 1.1},
                 headers=fresh)
    after_list = client2.get(f"/api/scenarios?project_id={project_id}",
                             headers=fresh).get_json()
    record("P-12", "A scenario created after the restart joins the earlier ones",
           after_list.get("total", 0) >= 2
           and any(s["id"] == scenario_id for s in after_list["scenarios"]),
           f"{after_list.get('total')} scenarios")

    # ---- P-13 Sign-out revokes across a restart ------------------------
    client2.post("/api/auth/logout", headers=headers)
    mod3 = build_app()
    client3 = mod3.app.test_client()
    revoked = client3.get("/api/auth/me", headers=headers)
    record("P-13", "A revoked session stays revoked after a restart",
           revoked.status_code == 401, f"HTTP {revoked.status_code}")

    # ---- P-14 Another user cannot see this project --------------------
    other = client3.post("/api/auth/signup", json={
        "name": "Other", "email": f"other-{uuid.uuid4().hex[:6]}@example.com",
        "password": password}).get_json()
    other_h = {"Authorization": f"Bearer {other.get('token')}"}
    leak = client3.get(f"/api/network/structure?project_id={project_id}",
                       headers=other_h)
    record("P-14", "Ownership is enforced on restored projects too",
           leak.status_code in (403, 404), f"HTTP {leak.status_code}")

    (OUT / "persistence_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    print(f"\n{passed} passed, {failed} failed of {len(results['checks'])}")
    print(f"database: {db_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
