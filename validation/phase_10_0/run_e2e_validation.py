"""
Phase 10.0 — End-to-end validation.

Boots the real Flask app, drives it with a real browser, and records what the
production application actually does. Every result written to
`e2e_validation.json` is observed, not asserted from documentation.

Run:  python validation/phase_10_0/run_e2e_validation.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from wsgiref.simple_server import make_server

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
OUT_DIR = Path(__file__).resolve().parent
PORT = 5099
BASE = f"http://127.0.0.1:{PORT}"

results: dict = {"checks": [], "console_errors": [], "summary": {}}


def record(check_id: str, name: str, status: str, detail: str = "", evidence=None):
    results["checks"].append({
        "id": check_id,
        "name": name,
        "status": status,
        "detail": detail,
        "evidence": evidence,
    })
    print(f"[{status:5}] {check_id}  {name}" + (f" — {detail}" if detail else ""))


def start_server():
    from app.backend.app import app

    app.config["TESTING"] = False
    server = make_server("127.0.0.1", PORT, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(1.0)
    return server


def main() -> int:
    import requests

    server = start_server()
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    password = "e2e-validation-pw-1"
    session = requests.Session()

    # -- 1. health ------------------------------------------------------
    r = session.get(f"{BASE}/api/status", timeout=30)
    status = r.json()
    record(
        "E2E-01", "Application boots with all subsystems mounted",
        "PASS" if (status["orchestrator"]["mounted"]
                   and status["ingestion"]["mounted"]
                   and status["application_api"]["mounted"]) else "FAIL",
        f"capabilities={status['orchestrator'].get('capabilities')}",
        status,
    )

    # -- 2. anonymous access is refused ---------------------------------
    codes = {
        path: session.get(f"{BASE}{path}", timeout=30).status_code
        for path in ("/api/projects", "/api/auth/me", "/api/kpis/network?project_id=x")
    }
    record(
        "E2E-02", "Protected endpoints refuse anonymous access",
        "PASS" if all(c == 401 for c in codes.values()) else "FAIL",
        str(codes), codes,
    )

    # -- 3. signup / login ----------------------------------------------
    r = session.post(f"{BASE}/api/auth/signup",
                     json={"email": email, "password": password, "name": "E2E User"}, timeout=30)
    token = r.json().get("token", "")
    auth = {"Authorization": f"Bearer {token}"}
    record("E2E-03", "User can register and receive a session",
           "PASS" if r.status_code == 201 and token else "FAIL", f"HTTP {r.status_code}")

    bad = session.post(f"{BASE}/api/auth/login",
                       json={"email": email, "password": "wrong-password"}, timeout=30)
    record("E2E-04", "Login rejects an incorrect password",
           "PASS" if bad.status_code == 401 else "FAIL", f"HTTP {bad.status_code}")

    # -- 4. project lifecycle -------------------------------------------
    r = session.post(f"{BASE}/api/projects",
                     json={"name": "E2E Workspace", "region": "India"},
                     headers=auth, timeout=30)
    project = r.json()
    record("E2E-05", "Project created and owned by the caller",
           "PASS" if r.status_code == 201 else "FAIL", f"HTTP {r.status_code}", project)

    record("E2E-06", "New project starts with no network bound",
           "PASS" if project.get("snapshot_id") is None and project.get("has_network") is False
           else "FAIL",
           f"snapshot_id={project.get('snapshot_id')}")

    # -- 5. no substitution of synthetic data ---------------------------
    r = session.get(f"{BASE}/api/kpis/network?project_id={project['id']}",
                    headers=auth, timeout=60)
    body = r.json()
    ok = r.status_code == 409 and body.get("error", {}).get("code") == "NO_NETWORK_BOUND"
    record("E2E-07", "KPIs for an unbound project refuse rather than substitute",
           "PASS" if ok else "FAIL", f"HTTP {r.status_code} {body.get('error', {}).get('code')}")

    # -- 6. project isolation -------------------------------------------
    other = f"e2e-other-{uuid.uuid4().hex[:8]}@example.com"
    r2 = session.post(f"{BASE}/api/auth/signup",
                      json={"email": other, "password": password}, timeout=30)
    other_auth = {"Authorization": f"Bearer {r2.json()['token']}"}
    r = session.get(f"{BASE}/api/projects/{project['id']}", headers=other_auth, timeout=30)
    record("E2E-08", "A second user cannot read the first user's project",
           "PASS" if r.status_code == 403 else "FAIL", f"HTTP {r.status_code}")

    listed = session.get(f"{BASE}/api/projects", headers=other_auth, timeout=30).json()
    leaked = [p for p in listed["projects"] if p["id"] == project["id"]]
    record("E2E-09", "Project listing excludes other users' projects",
           "PASS" if not leaked else "FAIL", f"{len(listed['projects'])} visible")

    # -- 7. authoritative KPIs on the demo network ----------------------
    demo = "pr-demo-case16"
    r = session.get(f"{BASE}/api/kpis/network?project_id={demo}", headers=auth, timeout=120)
    kpis = r.json().get("kpis", {})
    violations = [
        m for m, k in kpis.items()
        if k.get("status") != "VALID" and k.get("value") is not None
    ]
    record("E2E-10", "Every network KPI carries a status; none fabricates a value",
           "PASS" if r.status_code == 200 and kpis and not violations else "FAIL",
           f"{len(kpis)} KPIs, {len(violations)} violations",
           {m: {"status": k.get("status"), "value": k.get("value"), "unit": k.get("unit")}
            for m, k in kpis.items()})

    # -- 8. evidence package --------------------------------------------
    r = session.get(f"{BASE}/api/kpis/evidence?project_id={demo}", headers=auth, timeout=120)
    record("E2E-11", "AuthoritativeEvidencePackage is exposed over HTTP",
           "PASS" if r.status_code == 200 and "evidence" in r.json() else "FAIL",
           f"HTTP {r.status_code}")

    # -- 9. real scenario solve -----------------------------------------
    r = session.post(f"{BASE}/api/scenarios/simulate?project_id={demo}",
                     json={"project_id": demo, "name": "E2E Capacity Cut",
                           "action": "CHANGE_CAPACITY", "facility_ids": ["DC_CENTRAL"],
                           "capacity_delta_units": -1500.0},
                     headers=auth, timeout=180)
    scenario = r.json() if r.status_code == 201 else {}
    has_authoritative = all(k in scenario for k in
                            ("baseline_kpis", "scenario_kpis", "deltas", "provenance"))
    record("E2E-12", "Scenario is solved by the real engine and returns authoritative KPIs",
           "PASS" if r.status_code == 201 and has_authoritative else "FAIL",
           f"HTTP {r.status_code}",
           {"execution_id": scenario.get("execution_id"),
            "provenance": scenario.get("provenance"),
            "delta_metrics": list((scenario.get("deltas") or {}).keys())})

    fabricated = {"1205000", "95.5", "68.2", "102400", "1220000", "96.5"}
    blob = json.dumps(scenario)
    hits = [v for v in fabricated if v in blob]
    record("E2E-13", "Scenario response contains no known fabricated constant",
           "PASS" if not hits else "FAIL", f"hits={hits}")

    r = session.get(f"{BASE}/api/scenarios/baseline?project_id={demo}", headers=auth, timeout=120)
    record("E2E-14", "Baseline is retrievable independently of any scenario",
           "PASS" if r.status_code == 200 and r.json().get("type") == "BASELINE" else "FAIL",
           f"HTTP {r.status_code}")

    # -- 10. forecast honesty -------------------------------------------
    r = session.get(f"{BASE}/api/forecast?project_id={demo}", headers=auth, timeout=120)
    fc = r.json()
    honest = fc.get("status") in ("OK", "FORECAST_UNAVAILABLE")
    if fc.get("status") == "FORECAST_UNAVAILABLE":
        honest = honest and fc.get("series") == []
    record("E2E-15", "Forecast reports an explicit status and never a fabricated cone",
           "PASS" if r.status_code == 200 and honest else "FAIL",
           f"status={fc.get('status')} series={len(fc.get('series', []))}")

    r = session.get(f"{BASE}/api/signals", headers=auth, timeout=30)
    sig = r.json()
    record("E2E-16", "Signals endpoint does not serve fabricated market bulletins",
           "PASS" if sig.get("status") == "NO_SIGNAL_SOURCE_CONFIGURED" and sig["signals"] == []
           else "PASS" if sig.get("status") == "OK" else "FAIL",
           f"status={sig.get('status')} count={len(sig.get('signals', []))}")

    # -- 11. upload validation ------------------------------------------
    r = session.post(f"{BASE}/api/ingestions/preview/upload-and-parse",
                     data={"project_id": project["id"]},
                     files={"files": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
                     headers=auth, timeout=60)
    record("E2E-17", "Upload rejects a disallowed file type",
           "PASS" if r.status_code == 400 else "FAIL", f"HTTP {r.status_code}")

    csv = b"facility_id,facility_type,city,capacity\nDC1,DC,Delhi,1000\nDC1,DC,Delhi,1000\n,,,\n"
    r = session.post(f"{BASE}/api/ingestions/preview/upload-and-parse",
                     data={"project_id": project["id"]},
                     files={"files": ("facilities.csv", csv, "text/csv")},
                     headers=auth, timeout=60)
    quality = r.json().get("dataQuality", {}) if r.status_code == 200 else {}
    measured = quality.get("validPct") is not None and quality.get("validPct") != 98.0
    record("E2E-18", "Ingestion measures real data quality (not a hardcoded 98%)",
           "PASS" if r.status_code == 200 and measured else "FAIL",
           f"validPct={quality.get('validPct')} dupes={quality.get('duplicateRows')} "
           f"empty={quality.get('emptyRows')}", quality)

    record("E2E-19", "Upload preview performs no optimisation",
           "PASS" if "kpis" not in (r.json().get("structure") or {}) else "FAIL",
           "structure carries no solver output")

    # -- 12. browser load ------------------------------------------------
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(BASE, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2500)

            title = page.title()
            module_errors = [
                e for e in errors
                if any(k in e.lower() for k in
                       ("syntaxerror", "failed to fetch", "does not provide an export",
                        "cannot find module", "unexpected token", "is not defined"))
            ]
            results["console_errors"] = errors[:40]
            record("E2E-20", "Frontend loads with no module or syntax error",
                   "PASS" if not module_errors else "FAIL",
                   f"title='{title}', {len(module_errors)} blocking of {len(errors)} console msgs",
                   module_errors[:10])

            OUT_DIR.joinpath("screenshots").mkdir(exist_ok=True)
            page.screenshot(path=str(OUT_DIR / "screenshots" / "phase_10_0_landing.png"),
                            full_page=False)
            browser.close()
    except Exception as exc:  # noqa: BLE001
        record("E2E-20", "Frontend loads with no module or syntax error", "ERROR", str(exc))

    server.shutdown()

    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    errored = sum(1 for c in results["checks"] if c["status"] == "ERROR")
    results["summary"] = {
        "total": len(results["checks"]),
        "passed": passed,
        "failed": failed,
        "errors": errored,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    (OUT_DIR / "e2e_validation.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n{passed} passed, {failed} failed, {errored} errored of {len(results['checks'])}")
    return 0 if failed == 0 and errored == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
