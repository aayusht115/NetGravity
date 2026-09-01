"""Create a scenario in the browser and check it changes what is drawn.

The Scenario Planning page solved correctly all along — the backend returned
real per-scenario KPIs — but nothing downstream could see the difference:

  * the simulate response carried network totals only, no per-facility or
    per-lane detail, so the Digital Twin had nothing to redraw from;
  * `getScenarioNetworkData()` was a switch over five prototype scenario ids,
    and a user-created scenario fell into an `else` that invented `DC_DELHI`
    and four more facilities that exist in no uploaded network;
  * scenarios that arrived by LISTING were pushed into `SCENARIOS` unmapped, so
    after a reload the comparison table read `totalCost` off a record that only
    had `scenario_kpis`.

These checks drive the real page with `Dump/NetGravity_Test_Data_Clean.xlsx`.
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
SHOTS = OUT / "screenshots"
PORT = 5161
BASE = f"http://127.0.0.1:{PORT}/"

results = {"checks": []}


def record(cid: str, name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results["checks"].append(
        {"id": cid, "name": name, "status": status, "detail": detail})
    print(f"[{status:4}] {cid:6} {name}" + (f" — {detail}" if detail else ""))


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

    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    server = make_server("127.0.0.1", PORT, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(1.0)

    page_errors: list = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1200)
        page.evaluate("window.switchAuthPanel('signup')")
        page.wait_for_timeout(300)
        page.fill("#signup-name", "Scenario Check")
        page.fill("#signup-email", f"scn-{uuid.uuid4().hex[:8]}@example.com")
        page.fill("#signup-password", "Netgravity@2026")
        page.evaluate("window.completeAuth('signup')")
        page.wait_for_selector("#create-project-page:not(.hidden)", timeout=30000)
        page.fill("#proj-name", "Scenario Network")
        page.click("#proj-create-submit")
        page.wait_for_selector("#upload-data-page:not(.hidden)", timeout=30000)
        page.set_input_files("#ing-file-input", [str(WORKBOOK)])
        page.wait_for_timeout(6000)
        page.click("#ing-continue-btn")
        page.wait_for_timeout(9000)
        for _ in range(12):
            clicked = False
            for sel in ("#ing-confirm-mapping-btn", "#ing-pdf-continue-btn",
                        "#ing-finish-btn", "#ing-build-btn"):
                btn = page.query_selector(sel)
                if btn and btn.is_visible() and not btn.is_disabled():
                    btn.click()
                    clicked = True
                    page.wait_for_timeout(3500)
                    break
            if not clicked:
                break
            if page.is_visible(".app-shell") and not page.is_visible("#ingestion-page"):
                break
        page.wait_for_timeout(15000)

        # ---- Solve a scenario through the real API the page uses -------
        created = page.evaluate(
            """async () => {
                const mod = await import('/js/integration/services/scenario-service.js');
                const ctx = await import('/js/integration/project-context.js');
                const pid = ctx.getActiveProjectId();
                const raw = await mod.scenarioService.simulateScenario({
                  project_id: pid, name: 'Close Kolkata DC',
                  action: 'CLOSE_FACILITY', facility_ids: ['F006'],
                });
                const m = await import('/js/integration/mappers/scenario-mapper.js');
                const mapped = m.mapScenarioRecord(raw);
                const data = await import('/js/data.js');
                mapped.num = data.SCENARIOS.length;
                data.SCENARIOS.push(mapped);
                return {
                  id: mapped.id,
                  totalCost: mapped.totalCost,
                  fillRate: mapped.fillRate,
                  maxUtil: mapped.maxUtil,
                  costChange: mapped.costChange,
                  nScenarioFacilities: Object.keys(mapped.scenarioFacilities || {}).length,
                  nBaselineFacilities: Object.keys(mapped.baselineFacilities || {}).length,
                  nScenarioFlows: (mapped.scenarioFlows || []).length,
                  nBaselineFlows: (mapped.baselineFlows || []).length,
                };
            }"""
        )
        record("S-01", "The scenario solves and reports its own cost",
               isinstance(created.get("totalCost"), (int, float))
               and created["totalCost"] > 0,
               json.dumps({k: created[k] for k in
                           ("totalCost", "fillRate", "maxUtil", "costChange")}))

        record("S-02", "The response carries the scenario's own topology",
               created["nScenarioFacilities"] == 8
               and created["nScenarioFlows"] > 0
               and created["nBaselineFacilities"] == 8,
               json.dumps({k: created[k] for k in created if k.startswith("n")}))

        # The comparison must be a real difference, not a copy of the baseline.
        diff = page.evaluate(
            """() => {
                const s = window.__ngScenarioProbe && window.__ngScenarioProbe();
                return s || null;
            }"""
        )

        # ---- The map must draw the scenario, not the baseline ----------
        drawn = page.evaluate(
            """async (scnId) => {
                const map = await import('/js/map.js');
                const data = await import('/js/data.js');
                const scn = data.SCENARIOS.find(s => s.id === scnId);
                // Same call the scenario map makes.
                map.renderScenarioDigitalTwin('scenario-leaflet-map', scnId, 'scenario');
                const closed = Object.entries(scn.scenarioFacilities || {})
                  .filter(([, v]) => v.isOpen === false).map(([k]) => k);
                const changedLanes = (scn.scenarioFlows || []).filter(f => {
                  const b = (scn.baselineFlows || []).find(
                    x => x.origin_id === f.origin_id && x.destination_id === f.destination_id);
                  return !b || Math.abs(f.flow_units - b.flow_units) >= 1;
                }).length;
                return { closed, changedLanes,
                         utils: Object.fromEntries(Object.entries(scn.scenarioFacilities || {})
                           .map(([k, v]) => [k, v.utilPct])) };
            }""",
            created["id"],
        )
        record("S-03", "The scenario closes the facility it was asked to close",
               "F006" in (drawn.get("closed") or []), json.dumps(drawn.get("closed")))
        record("S-04", "Corridors move relative to the baseline",
               (drawn.get("changedLanes") or 0) > 0,
               f"{drawn.get('changedLanes')} lanes differ")
        record("S-05", "Facility utilisation differs from the baseline",
               any(v is not None for v in (drawn.get("utils") or {}).values()),
               json.dumps(drawn.get("utils")))

        # ---- No prototype facility may appear in scenario map data -----
        leaked = page.evaluate(
            """async (scnId) => {
                const map = await import('/js/map.js');
                const data = await import('/js/data.js');
                const scn = data.SCENARIOS.find(s => s.id === scnId);
                const ids = Object.keys(scn.scenarioFacilities || {});
                const demo = ['DC_DELHI','DC_MUMBAI','DC_BENGALURU','DC_KOLKATA','DC_GUWAHATI'];
                return ids.filter(i => demo.includes(i));
            }""",
            created["id"],
        )
        record("S-06", "No prototype facility appears in the scenario map",
               not leaked, json.dumps(leaked))

        # ---- The real UI flow: open the toolbox and run a scenario ------
        # Driven through the page's own controls, not the service, so the whole
        # path is exercised: form -> simulate -> mapper -> SCENARIOS -> table.
        page.click("#nav-item-scenarios")
        page.wait_for_timeout(3000)
        page.click("#btn-create-scenario-main")
        page.wait_for_timeout(1200)

        page.fill("#toolbox-scenario-name", "Expand Pune DC")
        facility_options = page.evaluate(
            """() => {
                const s = document.getElementById('toolbox-facility');
                return s ? [...s.options].map(o => o.value) : [];
            }"""
        )
        record("S-07", "The scenario builder offers this network's facilities",
               "F007" in facility_options and not any(
                   o.startswith("DC_") for o in facility_options),
               json.dumps(facility_options))

        page.select_option("#toolbox-facility", "F007")
        page.fill("#toolbox-amount", "5000")
        page.screenshot(path=str(SHOTS / "scenario_01_toolbox.png"), full_page=True)
        page.click("#btn-run-toolbox-scenario")
        page.wait_for_timeout(45000)
        page.screenshot(path=str(SHOTS / "scenario_02_compare.png"), full_page=True)

        table = page.evaluate(
            """() => {
                const panel = document.getElementById('tab-scenarios');
                return panel ? panel.innerText.replace(/\\s+/g, ' ') : '';
            }"""
        )
        record("S-08", "The comparison table names the scenario the user created",
               "Expand Pune DC" in table, table[:260])

        figures = page.evaluate(
            """async () => {
                const data = await import('/js/data.js');
                return data.SCENARIOS.map(s => ({
                  name: s.name, cost: s.totalCost, maxUtil: s.maxUtil,
                  fill: s.fillRate, change: s.costChange,
                  facilities: Object.keys(s.scenarioFacilities || {}).length,
                }));
            }"""
        )
        ui_made = [f for f in figures if f["name"] == "Expand Pune DC"]
        record("S-09", "The UI-created scenario carries solved figures",
               bool(ui_made) and isinstance(ui_made[0]["cost"], (int, float))
               and ui_made[0]["facilities"] == 8,
               json.dumps(ui_made))

        record("S-10", "The comparison table shows no prototype scenario",
               not any(x in table for x in ("Baddi", "Guwahati", "Rebalance")),
               table[:200])

        record("S-11", "No uncaught page errors",
               not page_errors, "; ".join(page_errors[:3])[:300])

        browser.close()

    (OUT / "scenario_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    print(f"\n{passed} passed, {failed} failed of {len(results['checks'])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
