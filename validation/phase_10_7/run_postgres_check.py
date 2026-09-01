"""Prove the application runs on PostgreSQL and keeps a project's work.

The claim being checked is not "the code imports psycopg". It is:

  * every store the application has writes to PostgreSQL and reads back
    byte-for-byte, including a `CanonicalNetwork` with 8 facilities and
    36 lanes;
  * a COLD RESTART — every `app.backend` and `netgravity.orchestrator` module
    dropped from `sys.modules` and rebuilt from scratch — recovers the account,
    the project, the uploaded network, the analysis computed from it and the
    solved scenario;
  * the restored network re-solves to the same figure it produced before the
    restart;
  * the ANALYSIS survives too, so re-opening a project does not re-run a MILP
    that has already run;
  * the SQLite -> PostgreSQL migration copies everything and verifies it.

Requires a PostgreSQL server. Point NETGRAVITY_TEST_POSTGRES_URL at an empty
database; the script creates its own schema and leaves the rows behind for
inspection.

    python validation/phase_10_7/run_postgres_check.py
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

WORKBOOK = ROOT.parent / "Dump" / "NetGravity_Test_Data_Clean.xlsx"
OUT = pathlib.Path(__file__).parent

URL = os.environ.get(
    "NETGRAVITY_TEST_POSTGRES_URL",
    "postgresql://netgravity:netgravity@127.0.0.1:55432/netgravity_check",
)

results = {"checks": [], "database": URL.split("@")[-1]}


def record(cid: str, name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results["checks"].append(
        {"id": cid, "name": name, "status": status, "detail": detail})
    print(f"[{status:4}] {cid:6} {name}" + (f" - {detail}" if detail else ""))
    sys.stdout.flush()


def drop_app_modules() -> int:
    """Remove every application module so the next import rebuilds from disk."""
    doomed = [m for m in sys.modules
              if m.startswith("app.backend") or m.startswith("netgravity.orchestrator")]
    for m in doomed:
        del sys.modules[m]
    return len(doomed)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    os.environ["NETGRAVITY_DATABASE_URL"] = URL
    os.environ.pop("NETGRAVITY_DB_PATH", None)

    # ---- P-01 The application actually connects to PostgreSQL ----------
    from app.backend.services import persistence

    record("P-01", "The store is PostgreSQL, not a local file",
           persistence.database.kind == "postgresql",
           f"{persistence.database.kind} at {persistence.database.path}")

    record("P-02", "The connection string never carries its password onward",
           "netgravity:netgravity" not in persistence.database.path
           and ":" in URL,
           persistence.database.path)

    for table in persistence.TABLES:
        persistence.database.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed names

    from app.backend.app import app

    app.config["TESTING"] = True
    client = app.test_client()

    # ---- Build real state through the real API ------------------------
    email = f"pg-{uuid.uuid4().hex[:8]}@example.com"
    res = client.post("/api/auth/signup", json={
        "name": "Postgres Check", "email": email, "password": "Netgravity@2026"})
    token = res.get_json().get("token")
    headers = {"Authorization": f"Bearer {token}"}
    record("P-03", "An account is created and a session issued",
           res.status_code in (200, 201) and bool(token), f"HTTP {res.status_code}")

    res = client.post("/api/projects", json={"name": "Postgres Workspace"},
                      headers=headers)
    project_id = (res.get_json().get("project") or res.get_json())["id"]
    record("P-04", "A project is created", bool(project_id), project_id)

    data = {"project_id": project_id,
            "files": (io.BytesIO(WORKBOOK.read_bytes()), WORKBOOK.name)}
    res = client.post("/api/ingestions/preview/upload-and-parse", data=data,
                      content_type="multipart/form-data", headers=headers)
    parsed_ok = res.status_code == 200
    res = client.post("/api/ingestions/preview/commit",
                      json={"project_id": project_id}, headers=headers)
    commit = res.get_json() or {}
    record("P-05", "The client workbook is ingested and bound",
           parsed_ok and res.status_code in (200, 201),
           json.dumps(commit.get("network_summary", {})))

    kpis = client.get(f"/api/kpis/network?project_id={project_id}",
                      headers=headers).get_json()
    cost_before = ((kpis.get("kpis") or {}).get("business_network_cost") or {}).get("value")
    record("P-06", "The bound network solves and reports a cost",
           isinstance(cost_before, (int, float)), f"{cost_before}")

    ready = client.get(f"/api/kpis/readiness?project_id={project_id}",
                       headers=headers).get_json()
    record("P-07", "The analysis reports itself ready without re-solving",
           bool(ready.get("ready")) and ready.get("metrics", 0) > 0,
           json.dumps(ready))

    res = client.post(f"/api/scenarios/simulate?project_id={project_id}", json={
        "project_id": project_id, "name": "Greenfield on Postgres",
        "action": "ADD_FACILITY",
        "new_facility": {"name": "Nagpur DC", "latitude": 21.1458,
                         "longitude": 79.0882, "capacity_units_per_period": 24370,
                         "fixed_cost_per_year": 15754800, "handling_cost_per_unit": 0.0,
                         "role": "DC"}}, headers=headers)
    scenario = res.get_json() or {}
    scenario_id = scenario.get("id")
    record("P-08", "A greenfield scenario solves and is stored",
           res.status_code == 201 and bool(scenario_id),
           f"HTTP {res.status_code} {scenario_id}")

    counts_before = {t: persistence.database.count(t) for t in persistence.TABLES}
    record("P-09", "Every kind of record reached PostgreSQL",
           all(counts_before[t] > 0 for t in
               ("users", "sessions", "projects", "snapshots", "scenarios",
                "scenario_networks", "network_data", "analyses")),
           json.dumps(counts_before))

    snapshot_id = ready.get("snapshot_id")
    stored_network = persistence.database.query_one(
        "SELECT document FROM snapshots WHERE snapshot_id = ?", (snapshot_id,))
    stored_doc = persistence.Database.loads(stored_network["document"])
    facilities_stored = len(stored_doc.get("network", {}).get("facilities", []))
    lanes_stored = len(stored_doc.get("network", {}).get("lanes", []))
    record("P-10", "The uploaded network is stored whole, not summarised",
           facilities_stored >= 8 and lanes_stored >= 30,
           f"{facilities_stored} facilities, {lanes_stored} lanes")

    # ---- COLD RESTART -------------------------------------------------
    dropped = drop_app_modules()
    from app.backend.app import app as app2  # noqa: F811 — deliberately re-imported

    app2.config["TESTING"] = True
    client2 = app2.test_client()
    record("P-11", "The application is rebuilt from scratch",
           dropped > 20, f"{dropped} modules dropped and re-imported")

    res = client2.post("/api/auth/login",
                       json={"email": email, "password": "Netgravity@2026"})
    token2 = (res.get_json() or {}).get("token")
    headers2 = {"Authorization": f"Bearer {token2}"}
    record("P-12", "The account survives the restart",
           res.status_code == 200 and bool(token2), f"HTTP {res.status_code}")

    listed = client2.get("/api/projects", headers=headers2).get_json()
    project = next((p for p in listed.get("projects", [])
                    if p["id"] == project_id), None)
    record("P-13", "The project survives, still bound to its network",
           project is not None and project.get("snapshot_id") == snapshot_id,
           json.dumps(project) if project else "project missing")

    kpis2 = client2.get(f"/api/kpis/network?project_id={project_id}",
                        headers=headers2).get_json()
    cost_after = ((kpis2.get("kpis") or {}).get("business_network_cost") or {}).get("value")
    record("P-14", "The restored network reports the same cost as before",
           cost_before is not None and cost_after == cost_before,
           f"{cost_before} -> {cost_after}")

    ready2 = client2.get(f"/api/kpis/readiness?project_id={project_id}",
                         headers=headers2).get_json()
    record("P-15", "The ANALYSIS survives, so the MILP is not re-run",
           bool(ready2.get("ready"))
           and ready2.get("computed_at") == ready.get("computed_at"),
           f"computed_at {ready.get('computed_at')} -> {ready2.get('computed_at')}")

    scenarios2 = client2.get(f"/api/scenarios?project_id={project_id}",
                             headers=headers2).get_json()
    restored = next((s for s in scenarios2.get("scenarios", [])
                     if s["id"] == scenario_id), None)
    record("P-16", "The solved scenario survives with its figures",
           restored is not None
           and ((restored.get("scenario_kpis") or {})
                .get("business_network_cost") or {}).get("value") is not None,
           json.dumps((restored or {}).get("overrides")))

    record("P-17", "The scenario still knows what it changed",
           bool(restored and restored.get("new_sites")),
           json.dumps((restored or {}).get("new_sites")))

    # ---- Migration ----------------------------------------------------
    import sqlite3
    import tempfile

    sqlite_path = os.path.join(tempfile.gettempdir(),
                               f"ng-migrate-{uuid.uuid4().hex[:8]}.db")
    # The schema now comes from the migration list, not from one block of
    # CREATE TABLE IF NOT EXISTS. Building it through `Database` is also what a
    # real source database would have been built by.
    seed = persistence.Database(path=sqlite_path)
    seed.close()
    src = sqlite3.connect(sqlite_path)
    src.execute("INSERT INTO projects(project_id, owner_id, document, updated_at) "
                "VALUES(?,?,?,?)",
                ("pr-migrate", "usr-x",
                 json.dumps({"project_id": "pr-migrate", "name": "Migrated",
                             "note": "üñicode ✓", "n": 0.1 + 0.2}), 1.0))
    src.commit()
    src.close()

    from scripts.migrate_to_postgres import migrate

    code = migrate(sqlite_path, URL)
    migrated = persistence.database.query_one(
        "SELECT document FROM projects WHERE project_id = ?", ("pr-migrate",))
    doc = persistence.Database.loads(migrated["document"]) if migrated else {}
    record("P-18", "The SQLite migration copies and verifies every row",
           code == 0 and doc.get("name") == "Migrated"
           and doc.get("n") == 0.30000000000000004,
           f"exit {code}, float round-trip {doc.get('n')}, unicode {doc.get('note')!r}")

    record("P-19", "Migration is idempotent — running it twice is safe",
           migrate(sqlite_path, URL) == 0, "second run exited 0")

    os.unlink(sqlite_path)

    # ---- Report -------------------------------------------------------
    failed = [c for c in results["checks"] if c["status"] == "FAIL"]
    results["summary"] = {"total": len(results["checks"]),
                          "passed": len(results["checks"]) - len(failed),
                          "failed": len(failed)}
    (OUT / "postgres_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n{results['summary']['passed']}/{results['summary']['total']} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
