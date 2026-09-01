"""Drive the Create Scenario modal in a browser, once per scenario type.

Every check here goes through the page's own controls — open the modal, click a
type card, fill the fields it renders, press Run Scenario — so the whole path is
exercised: form -> service -> API -> MILP -> mapper -> SCENARIOS -> comparison
table -> recommendation panel -> Digital Twin.

What it is checking for, in the user's own words:

  * "If I create a new scenario, it is not reflecting anywhere on the screen."
  * "We are supposed to allow multiple scenarios up to three. Right now only
    one scenario is getting created."
  * "The comparison table values are not getting updated for some cases, and
    the recommendation panel on the right side is also not getting updated."
  * "If I want to open a new facility, the target locations should be anywhere
    in India, not a list of existing DCs or plants."
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
PORT = 5171
BASE = f"http://127.0.0.1:{PORT}/"

results = {"checks": []}


def record(cid: str, name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results["checks"].append(
        {"id": cid, "name": name, "status": status, "detail": detail})
    print(f"[{status:4}] {cid:6} {name}" + (f" - {detail}" if detail else ""))
    sys.stdout.flush()


SNAPSHOT_JS = """async () => {
    const data = await import('/js/data.js');
    const table = document.getElementById('multi-scenario-table-wrap');
    const take = document.getElementById('scn-multi-take-card');
    const toggles = [...document.querySelectorAll('[data-map-scn-id]')]
      .map(b => b.textContent.trim());
    return {
      scenarios: data.SCENARIOS.map(s => ({
        id: s.id, name: s.name, cost: s.totalCost, fill: s.fillRate,
        change: s.costChange, maxUtil: s.maxUtil, risk: s.capacityRisk,
        transport: s.transportCost, fixed: s.fixedCost,
        unserved: s.unservedDemand, open: s.facilitiesOpen,
        newSites: (s.newSites || []).length,
        overrides: s.overrides || [],
      })),
      tableText: table ? table.innerText.replace(/\\s+/g, ' ') : '',
      takeText: take ? take.innerText.replace(/\\s+/g, ' ') : '',
      caption: (document.getElementById('scn-map-caption') || {}).innerText || '',
      toggles,
      chips: [...document.querySelectorAll('.scn-selected-chip')]
        .map(c => c.textContent.replace('\\u2715', '').trim()),
    };
}"""


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
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # ---- Sign up, create a project, upload the client workbook ------
        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1200)
        page.evaluate("window.switchAuthPanel('signup')")
        page.wait_for_timeout(300)
        page.fill("#signup-name", "Scenario UI")
        page.fill("#signup-email", f"scnui-{uuid.uuid4().hex[:8]}@example.com")
        page.fill("#signup-password", "Netgravity@2026")
        page.evaluate("window.completeAuth('signup')")
        page.wait_for_selector("#create-project-page:not(.hidden)", timeout=30000)
        page.fill("#proj-name", "Scenario UI Network")
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
        page.wait_for_timeout(18000)

        page.click("#nav-item-scenarios")
        page.wait_for_timeout(3000)

        # ---- U-01 The baseline exists as its own row -------------------
        snap = page.evaluate(SNAPSHOT_JS)
        baseline = next((s for s in snap["scenarios"] if s["id"] == "SCN_ACTUAL"), None)
        record("U-01", "A baseline row exists, built from the solved network",
               baseline is not None and isinstance(baseline.get("cost"), (int, float)),
               json.dumps(baseline) if baseline else "no SCN_ACTUAL in SCENARIOS")

        record("U-02", "The map offers a Baseline toggle",
               any("Baseline" in t for t in snap["toggles"]),
               json.dumps(snap["toggles"]))

        page.screenshot(path=str(SHOTS / "ui_00_empty.png"), full_page=True)

        # ---- Helper: run one scenario through the real modal ------------
        def create(cid: str, name: str, type_key: str, fill, expect_in_table=True):
            page.click("#btn-create-scenario-main")
            page.wait_for_timeout(900)
            page.click(f'.scn-type-card[data-type="{type_key}"]')
            page.wait_for_timeout(500)
            page.fill("#toolbox-scenario-name", name)
            fill()
            page.click("#btn-run-toolbox-scenario")
            # Real MILP solves; the client network takes ~20-40s per scenario.
            for _ in range(60):
                page.wait_for_timeout(2000)
                visible = page.is_visible("#modal-create-toolbox.visible")
                if not visible:
                    break
                if page.query_selector("#scn-creation-error"):
                    break
            page.wait_for_timeout(2500)
            err = page.query_selector("#scn-creation-error")
            if err:
                page.click("#btn-close-toolbox")
                page.wait_for_timeout(500)
                return {"error": err.inner_text()}
            return page.evaluate(SNAPSHOT_JS)

        # ---- U-03 CHANGE_CAPACITY --------------------------------------
        def fill_capacity():
            page.select_option("#toolbox-facility", index=1)
            page.fill("#toolbox-amount", "6000")

        snap = create("U-03", "Capacity boost", "CHANGE_CAPACITY", fill_capacity)
        ok = "error" not in snap and "Capacity boost" in snap["tableText"]
        record("U-03", "Change Capacity runs and appears in the comparison table",
               ok, snap.get("error", snap["tableText"][:200]))

        record("U-04", "The recommendation panel names the scenario just created",
               "error" not in snap and "Capacity boost" in snap["takeText"],
               snap.get("takeText", "")[:220])

        # ---- U-05 CLOSE_FACILITY ---------------------------------------
        def fill_close():
            page.select_option("#toolbox-facility", index=2)

        snap = create("U-05", "Close a DC", "CLOSE_FACILITY", fill_close)
        record("U-05", "Close Facility runs and appears alongside the first",
               "error" not in snap
               and "Close a DC" in snap["tableText"]
               and "Capacity boost" in snap["tableText"],
               snap.get("error", snap["tableText"][:240]))

        # ---- U-06 A NEW facility, anywhere in India --------------------
        def fill_new_site():
            page.select_option("#toolbox-open-mode", "NEW")
            page.wait_for_timeout(400)
            page.select_option("#toolbox-site-city", label="Nagpur")
            page.fill("#toolbox-site-name", "Nagpur greenfield DC")
            page.fill("#toolbox-site-capacity", "6000")

        # The target-location control must NOT be a list of existing sites.
        page.click("#btn-create-scenario-main")
        page.wait_for_timeout(800)
        page.click('.scn-type-card[data-type="OPEN_FACILITY"]')
        page.wait_for_timeout(600)
        open_form = page.evaluate(
            """() => {
                const cities = document.getElementById('toolbox-site-city');
                const lat = document.getElementById('toolbox-site-lat');
                const lng = document.getElementById('toolbox-site-lng');
                return {
                  hasCityList: !!cities,
                  cityCount: cities ? cities.options.length : 0,
                  hasLat: !!lat, hasLng: !!lng,
                  latEditable: lat ? !lat.disabled && !lat.readOnly : false,
                  cities: cities ? [...cities.options].slice(1, 6).map(o => o.text) : [],
                };
            }"""
        )
        page.screenshot(path=str(SHOTS / "ui_01_new_site_form.png"), full_page=True)
        record("U-06", "Open Facility offers locations anywhere in India, not existing sites",
               open_form["hasCityList"] and open_form["cityCount"] > 20
               and open_form["hasLat"] and open_form["hasLng"]
               and open_form["latEditable"],
               json.dumps(open_form))
        page.click("#btn-close-toolbox")
        page.wait_for_timeout(500)

        snap = create("U-07", "Nagpur greenfield DC", "OPEN_FACILITY", fill_new_site)
        created = next((s for s in snap.get("scenarios", [])
                        if s["name"] == "Nagpur greenfield DC"), None)
        record("U-07", "A greenfield site solves and is recorded as a new site",
               created is not None and created["newSites"] == 1,
               json.dumps(created) if created else snap.get("error", ""))

        # ---- U-08 Three scenarios are compared at once -----------------
        record("U-08", "Three scenarios are compared side by side",
               len(snap.get("chips", [])) == 3, json.dumps(snap.get("chips")))

        header_names = page.evaluate(
            """() => [...document.querySelectorAll('#multi-scenario-table-wrap thead th')]
                 .map(th => th.textContent.trim())"""
        )
        record("U-09", "The table has a baseline column plus three scenario columns",
               len(header_names) == 5 and "Baseline" in header_names[1],
               json.dumps(header_names))
        page.screenshot(path=str(SHOTS / "ui_02_three_scenarios.png"), full_page=True)

        # ---- U-10 Each scenario's OWN effect is separated ---------------
        # NOT "every scenario has a distinct cost": two different changes can
        # genuinely re-optimise to the same plan, and on this network they do —
        # a capacity increase at a facility that is not the binding constraint,
        # and a greenfield site the solver declines to open, both leave the
        # optimum exactly where it was. What must never happen is both claiming
        # a saving they did not produce, which is what the page did while the
        # only reference was an as-is baseline the scenario is free to redesign.
        effects = page.evaluate(
            """async () => {
                const data = await import('/js/data.js');
                const base = data.SCENARIOS.find(s => s.id === 'SCN_ACTUAL');
                return data.SCENARIOS.filter(s => s.id !== 'SCN_ACTUAL').map(s => ({
                  name: s.name, cost: s.totalCost, reference: s.referenceCost,
                  changeEffect: s.changeEffect,
                  reoptEffect: (typeof s.referenceCost === 'number' && base)
                    ? +(s.referenceCost - base.totalCost).toFixed(2) : null,
                }));
            }"""
        )
        reported = [e for e in effects if e["changeEffect"] is not None]
        no_effect = [e for e in reported if abs(e["changeEffect"]) < 1]
        table_says_so = page.evaluate(
            """() => {
                const rows = [...document.querySelectorAll('#multi-scenario-table-wrap tbody tr')];
                const row = rows.find(r => (r.querySelector('.scn-row2-label') || {})
                  .textContent?.includes("This change's own effect"));
                return row ? [...row.cells].slice(2).map(c => c.innerText.trim()) : null;
            }"""
        )
        record("U-10", "Each scenario reports its own effect, apart from the re-optimisation",
               len(reported) == len(effects) and table_says_so is not None
               and (not no_effect or any('No effect' in v for v in table_says_so)),
               json.dumps({"effects": effects, "row": table_says_so}))

        # ---- U-11 The comparison table has no blank metric rows --------
        cells = page.evaluate(
            """() => {
                const rows = [...document.querySelectorAll('#multi-scenario-table-wrap tbody tr')];
                return rows.map(r => ({
                  metric: (r.querySelector('.scn-row2-label') || {}).textContent
                            ? r.querySelector('.scn-row2-label').textContent.trim() : '',
                  values: [...r.cells].slice(1).map(c => c.innerText.split('\\n')[0].trim()),
                }));
            }"""
        )
        blank = [c for c in cells
                 if any(v in ('', 'undefined', 'NaN', 'NaN%', '₹NaN')
                        for v in c["values"])
                 or any(v in ('', '—') for v in c["values"][1:])]
        record("U-11", "No metric row in the comparison table is blank or NaN",
               not blank, json.dumps(blank)[:300])

        # ---- U-12 The extra cost rows are populated --------------------
        page.evaluate(
            """() => {
                document.getElementById('scn-multi-customize-btn').click();
                ['transportCost','fixedCost','handlingCost','inventoryCost',
                 'unservedDemand','facilitiesOpen'].forEach(k => {
                  const cb = document.querySelector(`[data-metric-key="${k}"]`);
                  if (cb && !cb.checked) cb.click();
                });
            }"""
        )
        page.wait_for_timeout(1200)
        cells = page.evaluate(
            """() => {
                const rows = [...document.querySelectorAll('#multi-scenario-table-wrap tbody tr')];
                return rows.map(r => ({
                  metric: (r.querySelector('.scn-row2-label') || {}).textContent
                            ? r.querySelector('.scn-row2-label').textContent.trim() : '',
                  values: [...r.cells].slice(1).map(c => c.innerText.split('\\n')[0].trim()),
                }));
            }"""
        )
        wanted = {'Transport Cost', 'Fixed Facility Cost', 'Handling Cost',
                  'Inventory Cost', 'Unserved Demand', 'Facilities Open'}
        shown = {c["metric"] for c in cells}
        empty = [c for c in cells if c["metric"] in wanted
                 and any(v in ('', '—', 'Unavailable', 'undefined') for v in c["values"])]
        record("U-12", "The cost-component rows carry real values on every column",
               wanted <= shown and not empty,
               json.dumps({"missing": sorted(wanted - shown),
                           "empty": [c["metric"] for c in empty]}))
        page.screenshot(path=str(SHOTS / "ui_03_all_metrics.png"), full_page=True)

        # ---- U-13 The recommendation ranks what is on screen -----------
        take = page.evaluate(
            "() => document.getElementById('scn-multi-take-card').innerText")
        names = [s["name"] for s in snap["scenarios"] if s["id"] != "SCN_ACTUAL"]
        record("U-13", "The recommendation panel names a scenario on screen and ranks the rest",
               any(n in take for n in names) and 'ALSO COMPARED' in take.upper(),
               take.replace("\n", " ")[:280])

        record("U-14", "The recommendation panel shows no undefined text",
               'undefined' not in take and 'NaN' not in take,
               take.replace("\n", " ")[:200])

        # ---- U-15 The map redraws per scenario -------------------------
        drawings = page.evaluate(
            """async () => {
                const out = {};
                const buttons = [...document.querySelectorAll('[data-map-scn-id]')];
                for (const btn of buttons) {
                  btn.click();
                  await new Promise(r => setTimeout(r, 700));
                  const container = document.getElementById('scenario-leaflet-map');
                  out[btn.textContent.trim()] = {
                    lines: container.querySelectorAll('path.leaflet-interactive').length,
                    markers: container.querySelectorAll('.custom-marker').length,
                    caption: (document.getElementById('scn-map-caption') || {}).innerText || '',
                  };
                }
                return out;
            }"""
        )
        line_counts = {k: v["lines"] for k, v in drawings.items()}
        record("U-15", "The Digital Twin draws a different network per scenario",
               len(set(line_counts.values())) > 1, json.dumps(line_counts))

        record("U-16", "The map says what each scenario changed",
               all(v["caption"].strip() for v in drawings.values()),
               json.dumps({k: v["caption"][:90] for k, v in drawings.items()}))

        # The greenfield scenario must draw the site it opens.
        greenfield = page.evaluate(
            """async () => {
                const btns = [...document.querySelectorAll('[data-map-scn-id]')];
                const btn = btns.find(b => b.textContent.includes('Nagpur'));
                if (!btn) return { found: false };
                btn.click();
                await new Promise(r => setTimeout(r, 900));
                const html = document.getElementById('scenario-leaflet-map').innerHTML;
                return { found: true, hasNewBadge: html.includes('NEW') };
            }"""
        )
        record("U-17", "A scenario that opens a new site draws that site on the map",
               greenfield.get("found") and greenfield.get("hasNewBadge"),
               json.dumps(greenfield))
        page.screenshot(path=str(SHOTS / "ui_04_greenfield_map.png"), full_page=True)

        # ---- U-18 The drawer shows solved evidence ---------------------
        page.evaluate("() => document.querySelector('[data-take-review]').click()")
        page.wait_for_timeout(1200)
        drawer = page.evaluate(
            "() => document.getElementById('scenario-drawer-content').innerText")
        record("U-18", "The scenario drawer shows what was asked and what the solver did",
               'What you asked for' in drawer and 'What the solver changed' in drawer
               and 'undefined' not in drawer,
               drawer.replace("\n", " ")[:260])
        page.screenshot(path=str(SHOTS / "ui_05_drawer.png"), full_page=True)
        page.evaluate(
            "() => document.getElementById('btn-close-scenario-drawer').click()")
        page.wait_for_timeout(600)

        # ---- U-19 CHANGE_DEMAND, which named no facility before --------
        def fill_demand():
            page.fill("#toolbox-amount", "20")

        snap = create("U-19", "Peak demand +20%", "CHANGE_DEMAND", fill_demand)
        record("U-19", "Change Demand runs without asking for a facility",
               "error" not in snap
               and any(s["name"] == "Peak demand +20%" for s in snap["scenarios"]),
               snap.get("error", "")[:200])

        # ---- U-20 CHANGE_TRANSPORT_COST --------------------------------
        def fill_freight():
            page.fill("#toolbox-amount", "25")

        snap = create("U-20", "Freight +25%", "CHANGE_TRANSPORT_COST", fill_freight)
        freight = next((s for s in snap.get("scenarios", [])
                        if s["name"] == "Freight +25%"), None)
        base = next((s for s in snap.get("scenarios", [])
                     if s["id"] == "SCN_ACTUAL"), None)
        record("U-20", "Change Transport Cost runs and freight cost actually rises",
               freight is not None and base is not None
               and freight["transport"] > base["transport"],
               json.dumps({"baseline": base and base["transport"],
                           "scenario": freight and freight["transport"]}))

        # ---- U-21 CHANGE_SLA -------------------------------------------
        def fill_sla():
            page.fill("#toolbox-amount", "1")

        snap = create("U-21", "Relax SLA by a day", "CHANGE_SLA", fill_sla)
        sla_scn = next((s for s in snap.get("scenarios", [])
                        if s["name"] == "Relax SLA by a day"), None)
        record("U-21", "Change SLA runs and reports its own solved figures",
               sla_scn is not None and isinstance(sla_scn["cost"], (int, float)),
               json.dumps(sla_scn) if sla_scn else snap.get("error", "")[:200])

        # ---- U-22 Six scenarios exist; three are compared --------------
        record("U-22", "Every scenario is kept, and exactly three are compared",
               len([s for s in snap["scenarios"] if s["id"] != "SCN_ACTUAL"]) >= 6
               and len(snap["chips"]) == 3,
               json.dumps({"solved": len(snap["scenarios"]) - 1,
                           "compared": snap["chips"]}))

        # ---- U-23 Swapping the comparison changes the table ------------
        before_table = page.evaluate(
            "() => document.getElementById('multi-scenario-table-wrap').innerText")
        swapped = page.evaluate(
            """async () => {
                document.querySelector('.scn-selected-chip [data-remove-id]').click();
                await new Promise(r => setTimeout(r, 500));
                document.getElementById('scn-add-scenario-btn').click();
                await new Promise(r => setTimeout(r, 400));
                const item = document.querySelector('[data-add-id]');
                const name = item ? item.textContent.replace('\\u2715','').trim() : null;
                if (item) item.click();
                await new Promise(r => setTimeout(r, 900));
                return { added: name };
            }"""
        )
        after_table = page.evaluate(
            "() => document.getElementById('multi-scenario-table-wrap').innerText")
        record("U-23", "Swapping a compared scenario changes the table",
               before_table != after_table
               and (swapped["added"] or '') in after_table,
               json.dumps(swapped))

        # ---- U-24 Deleting a scenario is permanent ---------------------
        deleted = page.evaluate(
            """async () => {
                const data = await import('/js/data.js');
                document.getElementById('scn-add-scenario-btn').click();
                await new Promise(r => setTimeout(r, 400));
                const del = document.querySelector('[data-del-id]');
                if (!del) return { skipped: true };
                const id = del.dataset.delId;
                del.click();
                await new Promise(r => setTimeout(r, 2500));
                const svc = await import('/js/integration/services/scenario-service.js');
                const server = await svc.scenarioService.listScenarios();
                return {
                  id,
                  goneLocally: !data.SCENARIOS.some(s => s.id === id),
                  goneOnServer: !server.some(s => s.id === id),
                };
            }"""
        )
        record("U-24", "A deleted scenario is removed on the server, not just on screen",
               deleted.get("goneLocally") and deleted.get("goneOnServer"),
               json.dumps(deleted))

        # ---- U-25 A refused scenario is refused, not shown as a result --
        refused = page.evaluate(
            """async () => {
                const svc = await import('/js/integration/services/scenario-service.js');
                try {
                  await svc.scenarioService.simulateScenario({
                    name: 'Impossible site', action: 'ADD_FACILITY',
                    new_facility: { name: 'Nowhere', latitude: 21, longitude: 79,
                                    capacity_units_per_period: 0 },
                  });
                  return { rejected: false };
                } catch (e) {
                  return { rejected: true, message: String(e.message || e) };
                }
            }"""
        )
        record("U-25", "A scenario the engine refuses is reported, not stored as a result",
               refused.get("rejected"), json.dumps(refused)[:240])

        # ---- U-26 Reload: the session and the scenarios survive --------
        # A refresh used to land on the marketing page with a valid token
        # sitting in localStorage, which reads as "everything I did is gone".
        page.reload(wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        restored = page.evaluate(
            """() => ({
                landingHidden: !!document.getElementById('landing-page')
                  ?.classList.contains('hidden'),
                shellVisible: (document.querySelector('.app-shell') || {}).style
                  ? document.querySelector('.app-shell').style.display === 'flex' : false,
            })"""
        )
        record("U-26", "A page refresh restores the signed-in session and project",
               restored["landingHidden"] and restored["shellVisible"],
               json.dumps(restored))
        page.wait_for_timeout(22000)
        page.click("#nav-item-scenarios")
        page.wait_for_timeout(4000)
        snap = page.evaluate(SNAPSHOT_JS)
        record("U-27", "Scenarios and the baseline survive a page reload",
               any(s["id"] == "SCN_ACTUAL" for s in snap["scenarios"])
               and len(snap["scenarios"]) > 3
               and all(v not in snap["tableText"] for v in ('undefined', 'NaN')),
               json.dumps({"count": len(snap["scenarios"]),
                           "chips": snap["chips"]}))
        page.screenshot(path=str(SHOTS / "ui_06_after_reload.png"), full_page=True)

        record("U-28", "No prototype facility or scenario appears anywhere",
               not any(x in snap["tableText"] + snap["takeText"]
                       for x in ("Baddi", "Guwahati", "Rebalance", "Delhi NCR DC")),
               snap["tableText"][:200])

        record("U-29", "No uncaught page errors",
               not page_errors, "; ".join(page_errors[:3])[:400])

        browser.close()

    (OUT / "scenario_ui_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    print(f"\n{passed} passed, {failed} failed of {len(results['checks'])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
