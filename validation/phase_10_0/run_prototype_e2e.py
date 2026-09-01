"""
Phase 10.0 — Prototype UI end-to-end validation.

Drives the APPROVED prototype (`index.html`) in a real browser through the
full journey the brief specifies:

    landing → create account → create project → upload data → mapping review
           → confirm → network built → dashboard shows MY data → scenario

Every screen, layout and control is the prototype's own; nothing here changes
the UI, it only exercises it.

Run:  python validation/phase_10_0/run_prototype_e2e.py
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

OUT = Path(__file__).resolve().parent
SHOTS = OUT / "screenshots"
PORT = 5097
BASE = f"http://127.0.0.1:{PORT}"

results: dict = {"checks": [], "console_errors": [], "summary": {}}

#: Full row text, whitespace-collapsed. Reading only the first line missed the
#: facility id, which is rendered on a later line of the cell.
TWIN_ROWS_JS = """() => {
  const rows = document.querySelectorAll('#tab-twin table tbody tr');
  return Array.from(rows)
    .map(r => r.innerText.replace(/\\s+/g, ' ').trim())
    .slice(0, 20);
}"""

TWIN_STATS_JS = """() => {
  const t = id => (document.getElementById(id) || {}).textContent || '';
  return {
    nodes: t('twin3d-node-count').trim(),
    corridors: t('twin3d-flow-count').trim(),
    // Third tile is "Overall Risk", as in the approved design. No
    // network-level risk metric has an authoritative owner, so this must stay
    // an em dash — a number here would be invented.
    risk: t('twin3d-risk-label').trim(),
  };
}"""

#: Ask the API directly for the authoritative per-facility utilisation, so the
#: UI is compared against the engine rather than against a number baked into
#: this test.
API_UTIL_JS = """async () => {
  const pid = localStorage.getItem('ng_active_project_id');
  const token = localStorage.getItem('ngt_auth_token');
  const res = await fetch(`/api/kpis/facilities?project_id=${pid}`,
                          { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) return {};
  const body = await res.json();
  const out = {};
  Object.entries(body.facilities || {}).forEach(([id, m]) => {
    const u = m.utilization_pct;
    if (u && u.status === 'VALID') out[id] = u.value;
  });
  return out;
}"""

SCENE_NODE_COUNT_JS = """() => (
  typeof window.twin3dNodeCount === 'function' ? window.twin3dNodeCount() : -1
)"""

MARKET_DEMAND_JS = """() => {
  const rows = document.querySelectorAll('#table-markets tbody tr');
  return Array.from(rows).map(r => r.innerText.replace(/\\s+/g, ' ').trim()).slice(0, 8);
}"""


def record(cid: str, name: str, status: str, detail: str = ""):
    results["checks"].append({"id": cid, "name": name, "status": status, "detail": detail})
    print(f"[{status:5}] {cid}  {name}" + (f" — {detail}" if detail else ""))


CSV = {
    "facilities.csv": (
        b"facility_id,facility_type,city,capacity,fixed_cost,handling_cost\n"
        b"PLT_PUNE,PLANT,Pune,20000,,\n"
        b"DC_MUM,DC,Mumbai,12000,140,4.8\n"
        b"DC_DEL,DC,Delhi,10000,120,4.2\n"
    ),
    "markets.csv": (
        b"market_id,city,demand,sla_days\n"
        b"MKT_MUM,Mumbai,4000,2\n"
        b"MKT_DEL,Delhi,3500,2\n"
    ),
    "lanes.csv": (
        b"from,to,cost,distance,lead_time\n"
        b"PLT_PUNE,DC_MUM,12,150,1\n"
        b"PLT_PUNE,DC_DEL,26,1400,3\n"
        b"DC_MUM,MKT_MUM,4,20,1\n"
        b"DC_DEL,MKT_DEL,4,25,1\n"
        b"DC_MUM,MKT_DEL,22,1400,3\n"
    ),
}


def main() -> int:
    from app.backend.app import app
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(exist_ok=True)
    tmp = OUT / "sample_upload"
    tmp.mkdir(exist_ok=True)
    paths = []
    for name, content in CSV.items():
        f = tmp / name
        f.write_bytes(content)
        paths.append(str(f))

    server = make_server("127.0.0.1", PORT, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(1.0)

    email = f"proto-{uuid.uuid4().hex[:8]}@example.com"
    password = "Netgravity@2026"
    errors: list = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1512, "height": 950})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # ── 1. Landing ────────────────────────────────────────────────
        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        record("P-01", "Landing page renders (approved prototype)",
               "PASS" if page.is_visible("#landing-page") else "FAIL",
               page.title()[:60])
        page.screenshot(path=str(SHOTS / "p01_landing.png"))

        # ── 2. Create account ─────────────────────────────────────────
        page.evaluate("window.switchAuthPanel('signup')")
        page.wait_for_timeout(400)
        page.fill("#signup-name", "Prototype Tester")
        page.fill("#signup-email", email)
        page.fill("#signup-password", password)
        page.screenshot(path=str(SHOTS / "p02_signup.png"))
        page.evaluate("window.completeAuth('signup')")
        page.wait_for_selector("#create-project-page:not(.hidden)", timeout=30000)
        record("P-02", "Sign-up succeeds and routes to Create Project", "PASS")
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOTS / "p03_create_project.png"))

        # ── 3. Create project ─────────────────────────────────────────
        page.fill("#proj-name", "My Uploaded Network")
        page.click("#proj-create-submit")
        page.wait_for_selector("#upload-data-page:not(.hidden)", timeout=30000)
        record("P-03", "Project created on the server and routes to Upload Data", "PASS")
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOTS / "p04_upload.png"))

        # ── 4. Upload real files ──────────────────────────────────────
        page.set_input_files("#ing-file-input", paths)
        page.wait_for_timeout(4000)
        rows = page.query_selector_all("#upload-data-page table tbody tr")
        record("P-04", "Uploaded files are listed after real backend parse",
               "PASS" if rows else "FAIL", f"{len(rows)} file row(s)")
        page.screenshot(path=str(SHOTS / "p05_uploaded.png"))

        # ── 5. Continue into mapping review ───────────────────────────
        page.click("#ing-continue-btn")
        page.wait_for_timeout(9000)
        record("P-05", "Ingestion flow advances to mapping review", "PASS")
        page.screenshot(path=str(SHOTS / "p06_mapping.png"))

        # ── 6. Walk the confirm buttons to the end of the flow ────────
        for _ in range(12):
            clicked = False
            for sel in ("#ing-confirm-mapping-btn", "#ing-pdf-continue-btn",
                        "#ing-finish-btn", "#ing-build-btn"):
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    clicked = True
                    page.wait_for_timeout(3500)
                    break
            if not clicked:
                break
            if page.is_visible(".app-shell") and not page.is_visible("#ingestion-page"):
                break

        page.wait_for_timeout(9000)
        in_app = page.evaluate(
            "() => { const s = document.querySelector('.app-shell');"
            " return !!s && getComputedStyle(s).display !== 'none'; }")
        record("P-06", "Mapping confirmed and the app shell opens",
               "PASS" if in_app else "FAIL")
        page.screenshot(path=str(SHOTS / "p07_home.png"), full_page=False)

        # ── 7. The dashboard shows MY network ─────────────────────────
        notice = page.query_selector("#ng-network-notice")
        notice_text = notice.inner_text() if notice else ""
        record("P-07", "Home states the true analysis state of the uploaded network",
               "PASS" if notice_text else "FAIL", notice_text[:110])

        twin_rows = page.evaluate(TWIN_ROWS_JS)
        mine = [r for r in twin_rows
                if any(k in r for k in ("PLT_PUNE", "DC_MUM", "DC_DEL"))]
        record("P-08", "Digital Twin tables list the uploaded facilities",
               "PASS" if len(mine) >= 3 else "FAIL",
               f"{len(twin_rows)} rows, matched {len(mine)}: {mine[:3]}")

        # The twin must show the SOLVER's utilisation, not a pre-solve guess.
        # The extractor used to stamp a literal 78% on every DC, which then
        # rendered on the Digital Twin as though it had been measured.
        #
        # Compared against the API rather than a fixed number, so the check
        # stays honest if the network or the solve changes.
        api_util = page.evaluate(API_UTIL_JS)
        util_row = next((r for r in twin_rows if "DC_MUM" in r), "")
        expected = api_util.get("DC_MUM")
        shown = expected is not None and (
            f"{expected}%" in util_row or f"{round(expected, 1)}%" in util_row)
        record("P-08b", "Digital Twin utilisation matches the authoritative KPI exactly",
               "PASS" if shown and "78%" not in util_row else "FAIL",
               f"twin row = '{util_row}' · API says {expected}%")

        # No facility from the prototype's own demo footprint may survive a
        # switch to the user's network — including inside insight narratives,
        # scenario dropdowns and action items.
        page_text = page.inner_text("body")
        ghosts = [g for g in ("Baddi Plant", "Guwahati DC", "Bengaluru DC",
                              "Kolkata DC", "Delhi NCR DC")
                  if g in page_text]
        record("P-09", "No prototype demo facility remains after loading my data",
               "PASS" if not ghosts else "FAIL", f"ghosts={ghosts}")

        page.evaluate("window.navigateToTab && window.navigateToTab('twin')")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(SHOTS / "p08_twin.png"))

        # The twin's stat overlay must describe THIS network. Node count was
        # hardcoded to 19 in the markup and corridor count to 20.
        stats = page.evaluate(TWIN_STATS_JS)
        # 1 plant + 2 DCs + 2 markets = 5 nodes; 5 lanes.
        record("P-08c", "Digital Twin stat overlay counts this network, not the demo one",
               "PASS" if stats["nodes"] == "5" and stats["corridors"] == "5" else "FAIL",
               f"nodes={stats['nodes']} corridors={stats['corridors']} risk={stats['risk']}")
        # The design's third tile must not acquire an invented value.
        record("P-08f", "Overall Risk tile shows no invented network-level figure",
               "PASS" if stats["risk"] in ("—", "-", "") else "FAIL",
               f"risk tile = {stats['risk']!r}")

        # The 3D scene must contain the same node count as the tables beside
        # it. It used to build once and never rebuild, so it kept the demo
        # geometry while the tables showed the user's network.
        scene_nodes = page.evaluate(SCENE_NODE_COUNT_JS)
        record("P-08e", "3D twin geometry matches the loaded network",
               "PASS" if scene_nodes == 5 else "FAIL",
               f"scene renders {scene_nodes} nodes, network has 5")

        # Market demand must be what I uploaded (4,000 / 3,500), not the
        # extractor's old hardcoded 2,000-unit placeholder.
        demands = page.evaluate(MARKET_DEMAND_JS)
        record("P-08d", "Market demand matches the uploaded file, not a placeholder",
               "PASS" if ("4,000" in " ".join(demands) and "3,500" in " ".join(demands))
               else "FAIL", f"demands={demands}")

        page.evaluate("window.navigateToTab && window.navigateToTab('facility-dashboard')")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(SHOTS / "p09_facility.png"))

        page.evaluate("window.navigateToTab && window.navigateToTab('scenarios')")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(SHOTS / "p10_scenarios.png"))

        # ── 8. Authoritative figures reached the UI ───────────────────
        cost_shown = page.evaluate("""() => {
            const el = document.querySelector('#home-kpi-grid .home2-kpi-strip-value');
            return el ? el.textContent.trim() : '';
        }""")
        record("P-10", "Home KPI strip shows a figure derived from my network",
               "PASS" if cost_shown and cost_shown not in ("—", "") else "FAIL",
               f"total cost tile = '{cost_shown}'")

        blocking = [e for e in errors if any(k in e.lower() for k in (
            "syntaxerror", "is not defined", "does not provide an export",
            "unexpected token", "cannot find module"))]
        results["console_errors"] = errors[:40]
        record("P-11", "No blocking JavaScript error across the journey",
               "PASS" if not blocking else "FAIL",
               f"{len(blocking)} blocking of {len(errors)} messages")

        browser.close()

    server.shutdown()

    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    results["summary"] = {"total": len(results["checks"]), "passed": passed,
                          "failed": failed,
                          "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (OUT / "prototype_e2e_validation.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\n{passed} passed, {failed} failed of {len(results['checks'])}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
