"""Greenfield siting, the loading screen, and values that came from nowhere.

Three questions, all driven through the real application:

  * "Unable to create a scenario where a new facility is opened." The scenario
    was created every time; the new facility could never carry a single unit
    and so was never opened. Two independent causes, both checked here.

  * "The loading screen should load until all the KPIs have been calculated."
    It did not exist on this path: the dashboard appeared first and the figures
    arrived afterwards. The check asserts the screen is never visible with an
    empty model behind it.

  * "Do not keep any hardcoded values." Each check below names a specific
    figure that was on screen and did not come from the user's data.

    python validation/phase_10_7/run_greenfield_and_hardcoded_check.py
"""

from __future__ import annotations

import io
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
PORT = 5173
BASE = f"http://127.0.0.1:{PORT}/"

results = {"checks": []}


def record(cid: str, name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results["checks"].append(
        {"id": cid, "name": name, "status": status, "detail": detail})
    print(f"[{status:4}] {cid:6} {name}" + (f" - {detail}" if detail else ""))
    sys.stdout.flush()


# ===========================================================================
# Part 1 — the engine, without a browser
# ===========================================================================

def engine_checks() -> str:
    """Returns the snapshot_id of the ingested client network."""
    import statistics

    from netgravity.optimization.milp import milp_solve
    from netgravity.schemas.network import (
        CanonicalNetwork, FacilityRecord, FacilityStatus, NodeRole,
        OptimizationConfig,
    )
    from netgravity.schemas.scenario import FacilityChange, Scenario
    from netgravity.scenarios.engine import ScenarioEngine
    from netgravity.scenarios.tariff import derive_lane_tariff
    from netgravity.tests.fixtures.case16_synthetic import build_case16_network

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
    email = f"gf-{uuid.uuid4().hex[:8]}@example.com"
    token = client.post("/api/auth/signup", json={
        "name": "Greenfield", "email": email,
        "password": "Netgravity@2026"}).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/projects", json={"name": "Greenfield Check"},
                          headers=headers).get_json()
    project_id = (created.get("project") or created)["id"]
    client.post("/api/ingestions/preview/upload-and-parse",
                data={"project_id": project_id,
                      "files": (io.BytesIO(WORKBOOK.read_bytes()), WORKBOOK.name)},
                content_type="multipart/form-data", headers=headers)
    client.post("/api/ingestions/preview/commit",
                json={"project_id": project_id}, headers=headers)

    from app.backend.services.project_registry import project_registry
    snapshot_id = project_registry.snapshot_for(
        project_id, user_id=client.get("/api/auth/me", headers=headers)
        .get_json()["user"]["id"])
    from app.backend.app import _orchestrator
    network = _orchestrator.snapshots.get(snapshot_id).network

    # ---- G-01 The tariff reproduces the client's own lanes -------------
    tariff = derive_lane_tariff(network.lanes)
    road = [l for l in network.lanes
            if getattr(l.mode, "value", str(l.mode)) == "ROAD"
            and l.distance_km > 0 and l.rate_per_unit > 0]
    #: The estimator that shipped: the mean of each lane's rate/distance ratio.
    old_per_km = sum(l.rate_per_unit / l.distance_km for l in road) / len(road)
    old_err = statistics.median(
        [abs(old_per_km * l.distance_km - l.rate_per_unit) / l.rate_per_unit
         for l in road]) * 100
    new_err = tariff.rate_fit_error_pct
    record("G-01", "The derived tariff reproduces this network's own freight",
           tariff.is_derived and new_err < 15 and old_err > 100,
           f"was median {old_err:.0f}% error, now {new_err:.1f}% "
           f"({tariff.fixed_leg_cost:,.2f}/unit + {tariff.rate_per_km:.5f}/unit-km)")

    record("G-02", "Transit for a new lane comes from the network, not 500 km/day",
           tariff.speed_km_per_day > 0
           and abs(tariff.speed_km_per_day - 500.0) > 1.0,
           f"{tariff.speed_km_per_day:,.0f} km/day + "
           f"{tariff.terminal_time_days:.2f} d terminal, "
           f"{tariff.lead_time_fit_error_pct:.1f}% median error")

    def solve_with_site(name, lat, lng, capacity, fixed, handling):
        site = FacilityRecord(
            id="NEW_SITE", name=name, role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE, latitude=lat, longitude=lng,
            capacity_units_per_period=capacity, fixed_cost_per_year=fixed,
            handling_cost_per_unit=handling, is_closable=True,
            is_mandatory=False, is_forced_closed=False)
        scenario = Scenario(scenario_id="S", scenario_name=name,
                            facility_changes=[FacilityChange(
                                facility_id="NEW_SITE", action="ADD_FACILITY",
                                new_facility=site)])
        modified = ScenarioEngine()._apply_overrides(network, scenario)  # noqa: SLF001
        res = milp_solve(modified, OptimizationConfig(allow_shortage=True)).model_dump()
        decision = next(f for f in res["facility_decisions"]
                        if f["facility_id"] == "NEW_SITE")
        return decision, res, modified

    baseline = milp_solve(network, OptimizationConfig(allow_shortage=True)).model_dump()
    base_fill = baseline["service_report"]["pct_demand_in_sla"]

    free, free_res, modified = solve_with_site(
        "Free DC on the unserved market", 18.5204, 73.8567, 100000, 0, 0.0)
    record("G-03", "A greenfield DC can carry flow at all",
           free["throughput_units"] > 0,
           f"open={free['is_open']} throughput={free['throughput_units']:,.0f} "
           f"(was 0 for every greenfield site, whatever its capacity)")

    site_record = next(f for f in modified.facilities if f.id == "NEW_SITE")
    record("G-04", "A new DC is not given a production capacity of zero",
           site_record.production_capacity_units_per_period > 1e11,
           f"production_capacity_units_per_period="
           f"{site_record.production_capacity_units_per_period:,.0f} "
           f"(was 0.0, and the MILP takes the smaller of the two limits)")

    real, real_res, _ = solve_with_site("Nagpur DC", 21.1458, 79.0882,
                                        20000, 25_000_000, 12.0)
    fill = real_res["service_report"]["pct_demand_in_sla"]
    record("G-05", "A realistic new site opens and serves more demand",
           real["is_open"] and fill > base_fill,
           f"fill {base_fill:.2f}% -> {fill:.2f}%, "
           f"throughput {real['throughput_units']:,.0f}")

    # ---- G-06 The solver still declines a site that does not pay -------
    # A synthetic network with a small, cheap footprint: a site priced far
    # above it must stay shut, or "it opens" would be a property of the fix
    # rather than of the economics.
    demo = build_case16_network()
    demo_scenario = Scenario(
        scenario_id="S", scenario_name="Absurd",
        facility_changes=[FacilityChange(
            facility_id="NEW_ABSURD", action="ADD_FACILITY",
            new_facility=FacilityRecord(
                id="NEW_ABSURD", name="Absurd", role=NodeRole.DC,
                status=FacilityStatus.CANDIDATE, latitude=52.2, longitude=-1.5,
                capacity_units_per_period=5000,
                fixed_cost_per_year=5_000_000_000.0,
                handling_cost_per_unit=5000.0,
                is_closable=True, is_mandatory=False))])
    demo_mod = ScenarioEngine()._apply_overrides(demo, demo_scenario)  # noqa: SLF001
    demo_res = milp_solve(demo_mod, demo.config).model_dump()
    absurd = next(f for f in demo_res["facility_decisions"]
                  if f["facility_id"] == "NEW_ABSURD")
    record("G-06", "A site that does not pay for itself is still declined",
           absurd["is_open"] is False,
           f"open={absurd['is_open']} at 5bn/yr fixed and 5,000/unit handling")

    # ---- G-07 No invented tariff when the network cannot price one ----
    thin = CanonicalNetwork(
        network_id="THIN", facilities=network.facilities[:3],
        products=network.products, demands=[], lanes=[], config=network.config)
    thin_tariff = derive_lane_tariff(thin.lanes)
    record("G-07", "A network with no comparable lanes is refused, not defaulted",
           not thin_tariff.is_derived and "0.025" not in thin_tariff.reason,
           thin_tariff.reason[:120])

    return snapshot_id, project_id, email


# ===========================================================================
# Part 2 — the browser
# ===========================================================================

def browser_checks() -> None:
    from app.backend.app import app
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    server = make_server("127.0.0.1", PORT, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(1.0)

    failed_requests: list = []
    page_errors: list = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        # The message AND the first frames of the stack. A bare
        # "Cannot read properties of null" names neither the file nor the line,
        # so a failure here said only that something broke somewhere — which
        # costs more to chase than the check saves.
        page.on("pageerror", lambda e: page_errors.append(
            f"{e}  @ {' | '.join((getattr(e, 'stack', '') or '').splitlines()[1:4])}"))
        page.on("response", lambda r: failed_requests.append(
            (r.status, r.url)) if r.status >= 400 else None)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)

        # ---- H-01 The sign-in form ships no fake saved password --------
        pw_value = page.eval_on_selector("#signin-password", "el => el.value")
        record("H-01", "The password field is not pre-filled with bullet characters",
               pw_value == "",
               f"value={pw_value!r} (shipped as '••••••••••••', which submits "
               f"as that literal string if the user does not clear it)")

        record("H-02", "Nothing 401s before anyone has signed in",
               not [r for r in failed_requests if r[0] == 401],
               json.dumps([r[1] for r in failed_requests][:3])
               or "no failed requests")

        page.evaluate("window.switchAuthPanel('signup')")
        page.wait_for_timeout(300)
        page.fill("#signup-name", "Phase 107")
        page.fill("#signup-email", f"p107-{uuid.uuid4().hex[:8]}@example.com")
        page.fill("#signup-password", "Netgravity@2026")
        page.evaluate("window.completeAuth('signup')")
        page.wait_for_selector("#create-project-page:not(.hidden)", timeout=30000)
        page.fill("#proj-name", "Phase 10.7 Network")
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
        page.wait_for_timeout(20000)

        # ---- The loading screen, on the ordinary open-a-project path ---
        page.evaluate("window.showSelectProject && window.showSelectProject()")
        page.wait_for_selector("#select-project-page:not(.hidden)", timeout=20000)
        page.wait_for_timeout(1200)

        owners = page.evaluate(
            """() => [...document.querySelectorAll('.proj-chip-owner')]
                 .map(e => e.textContent.trim())""")
        record("H-03", "A workspace is labelled by who actually owns it",
               "You" in owners and "Sample" in owners,
               json.dumps(owners) + "  (both branches of the previous "
               "expression returned 'You', so the shared demo workspace was "
               "listed as the viewer's own)")

        page.get_by_text("Open project").first.click()

        samples, overlay_texts = [], []
        saw_overlay = False
        for _ in range(400):
            state = page.evaluate("""() => {
                const o = document.getElementById('loading-modal-overlay');
                const shell = document.querySelector('.app-shell');
                return {
                  overlay: !!(o && o.classList.contains('active')),
                  text: o ? o.innerText.replace(/\\s+/g,' ') : '',
                  shell: !!shell && getComputedStyle(shell).display !== 'none',
                  cost: (typeof window.__ngModelProbe === 'function')
                    ? window.__ngModelProbe().baselineCost : null,
                };
            }""")
            samples.append(state)
            if state["overlay"]:
                saw_overlay = True
                if state["text"]:
                    overlay_texts.append(state["text"])
                page.screenshot(path=str(SHOTS / "p107_loading.png"))
            elif saw_overlay:
                break
            page.wait_for_timeout(80)

        record("L-01", "A loading screen holds the app while the analysis runs",
               saw_overlay, f"{len(samples)} samples taken")

        exposed = [s for s in samples
                   if s["shell"] and not s["overlay"] and s["cost"] is None]
        record("L-02", "The dashboard is never shown with no figures behind it",
               not exposed,
               f"{len(exposed)} sample(s) had the shell visible, the loading "
               f"screen down and no solved cost in the model")

        last = overlay_texts[-1] if overlay_texts else ""
        record("L-03", "The loading screen reports real work, not a timer",
               "facilities" in last and "KPIs computed" in last
               and "steps complete" in last,
               last[:200])

        page.wait_for_timeout(3000)
        page.screenshot(path=str(SHOTS / "p107_home.png"), full_page=True)

        home = page.evaluate("""() => {
            const t = (s) => { const e = document.querySelector(s);
                               return e ? e.innerText.replace(/\\s+/g,' ').trim() : ''; };
            return {
              body: document.body.innerText.replace(/\\s+/g,' '),
              refresh: t('.home2-refresh-text'),
              strip: t('.home2-kpi-strip'),
              periods: [...(document.getElementById('home-top-period')?.options || [])]
                         .map(o => o.text),
              periodDisabled: !!document.getElementById('home-top-period')?.disabled,
              callout: t('.home-twin-callout-sub'),
            };
        }""")

        record("H-04", "No fabricated service target is reported against",
               "vs target" not in home["strip"],
               home["strip"][:200] + "  (was 'vs target: -18.6%' against a 95% "
               "benchmark stated in no upload)")

        record("H-05", "The fill rate says what it is made of, from the solve",
               "units served" in home["strip"], home["strip"][:180])

        # This check used to require exactly `["Period 1"]`, disabled.
        #
        # That was a faithful description of what the app did, and the thing
        # the user reported as broken: the assembler keeps only the latest
        # period of an uploaded demand history, so the control had one option
        # and disabled itself. Asserting that state froze the defect in place —
        # a test can describe a bug precisely and still be defending it.
        #
        # The control now lists the periods the upload's own capacity history
        # records, which the collapse never touched. The requirement is still
        # that every option comes from the data: a label must look like a
        # period the client stated, and the four invented quarters must never
        # return. H-07 below continues to search the whole page for those.
        real_periods = [p for p in home["periods"] if p and p != "Period 1"]
        record("H-06", "The period control lists periods the upload states",
               len(home["periods"]) > 1 and not home["periodDisabled"]
               and bool(real_periods)
               and all(any(ch.isdigit() for ch in p) for p in home["periods"]),
               f"{len(home['periods'])} options "
               f"{json.dumps(home['periods'][:3])}…{json.dumps(home['periods'][-1:])} "
               f"disabled={home['periodDisabled']}"
               + "  (was a single disabled 'Period 1'; before that, Q3 2026 / "
               "Q2 2026 / Q1 2026 / Q4 2025, none of which appear in any upload)")

        # ---- The period control drives something ----------------------
        #
        # H-06 asserts the control OFFERS real periods. It offered thirty-six
        # real months while every one of them resolved to the same horizon
        # average, so the control moved and nothing behind it did — a filter
        # that looks like it works is worse than one that is visibly disabled.
        # This asserts that the periods the model solved return their OWN
        # solved utilisation, and that they differ from each other.
        # Across EVERY distribution centre, not just the first.
        #
        # A genuinely flat site is a real answer — F004 sits at its SLA-eligible
        # lane capacity of 3,230 units in all twelve periods, so one distinct
        # reading is correct for it and asserting otherwise would demand a
        # variation the data does not contain. What must be true is that at
        # least one site varies, because a horizon in which nothing varies is
        # indistinguishable from a control that still returns one number.
        horizon = page.evaluate("""async () => {
          const mod = await import('./js/data.js');
          const labels = Object.values(mod.SOLVE_HORIZON.periodLabels || {});
          const byFacility = {};
          mod.DCS.forEach((dc) => {
            byFacility[dc.id] = labels.map((p) => {
              const k = mod.getKpisForFacility(dc.id, p);
              return (k && k.utilisation) ? k.utilisation.value : null;
            });
          });
          return { periods: mod.SOLVE_HORIZON.periodsModelled,
                   labels: labels.length, byFacility,
                   costPerPeriod: mod.SOLVE_HORIZON.costPerPeriod };
        }""")
        series = {
            fid: [v for v in vals if v is not None]
            for fid, vals in (horizon.get("byFacility") or {}).items()
        }
        varying = {fid: vals for fid, vals in series.items() if len(set(vals)) > 1}
        complete = [fid for fid, vals in series.items()
                    if len(vals) == horizon.get("labels", 0)]
        widest = max(varying.items(), key=lambda kv: max(kv[1]) - min(kv[1]),
                     default=(None, []))
        record("H-14", "Choosing a period changes the solved figure behind it",
               horizon.get("periods", 1) > 1 and bool(varying)
               and len(complete) == len(series),
               f"{len(varying)} of {len(series)} site(s) vary across "
               f"{horizon.get('periods')} modelled periods; widest is "
               f"{widest[0]} at {min(widest[1]) if widest[1] else '—'}%–"
               f"{max(widest[1]) if widest[1] else '—'}%; "
               f"{len(complete)}/{len(series)} carry a reading for every period"
               + "  (every period returned the same horizon average until the "
               "solve's own per-period series was carried through)")

        record("H-15", "A per-period cost is published, not divided in the browser",
               isinstance(horizon.get("costPerPeriod"), (int, float))
               and horizon["costPerPeriod"] > 0,
               f"cost_per_period={horizon.get('costPerPeriod')}"
               + "  (the KPI layer's own figure; a cost divided in the UI "
               "would be a second cost engine)")

        record("H-07", "No hardcoded quarter appears anywhere on screen",
               "Q3 2026" not in home["body"] and "Q4 2025" not in home["body"],
               "searched the whole rendered page")

        record("H-08", "Per-period quantities carry the engine's own period",
               "units/month" in home["callout"] and "units/day" not in home["body"],
               home["callout"] + "  (the model's cost period is MONTH; every "
               "figure was labelled units/day)")

        record("H-09", "The analysis timestamp is real, not the literal '5 min ago'",
               "not yet analysed" not in home["refresh"]
               and home["refresh"].startswith("Last analysed"),
               home["refresh"])

        # ---- Digital Twin: solver status, not a green tag --------------
        page.click("#nav-item-twin")
        page.wait_for_timeout(3000)
        twin = page.evaluate(
            """() => [...document.querySelectorAll('#table-plants tbody tr')]
                 .map(r => r.innerText.replace(/\\s+/g,' '))""")
        record("H-10", "Facility status is the solver's decision, not always 'Active'",
               all("Active" not in row for row in twin)
               and any("Open" in row or "Closed" in row for row in twin),
               json.dumps(twin[:3]))
        page.screenshot(path=str(SHOTS / "p107_twin.png"), full_page=True)

        # ---- The greenfield form, end to end ---------------------------
        page.click("#nav-item-scenarios")
        page.wait_for_timeout(3000)
        page.click("#btn-create-scenario-main")
        page.wait_for_timeout(900)
        page.click('.scn-type-card[data-type="OPEN_FACILITY"]')
        page.wait_for_timeout(700)
        form = page.evaluate("""() => {
            const v = i => { const e = document.getElementById(i); return e ? e.value : null; };
            const city = document.getElementById('toolbox-site-city');
            return { name: v('toolbox-site-name'), lat: v('toolbox-site-lat'),
                     lng: v('toolbox-site-lng'), capacity: v('toolbox-site-capacity'),
                     fixed: v('toolbox-site-fixed'), handling: v('toolbox-site-handling'),
                     city: city ? city.options[city.selectedIndex].text : null };
        }""")
        page.screenshot(path=str(SHOTS / "p107_form.png"))
        # This required the name field to be EMPTY, as proof that the form
        # proposed no particular site. It was proof of that, and it was also
        # the defect users hit: every other field pre-filled from the network,
        # the name did not, and pressing Run returned "Give the new site a
        # name." without sending a request — reported as being unable to create
        # a scenario at all.
        #
        # The invariant was never "the name is blank"; it was "the product does
        # not invent a place or a figure". A generic label satisfies that — it
        # names no location and changes no number — so the check now tests the
        # thing it meant: the coordinates and capacity come from the loaded
        # network, and the name is not one of the cities the picker offers.
        preset_cities = page.evaluate("""() => [...document.querySelectorAll(
            '#toolbox-site-city option')].map(o => o.text.trim()).filter(Boolean)""")
        names_a_place = any(
            c.lower() in (form["name"] or "").lower()
            for c in preset_cities if c and "choose a city" not in c.lower())
        record("H-11", "The new-site form opens on the network, not on a city we chose",
               bool(form["name"])                      # runnable as it opens
               and not names_a_place                   # but proposes no location
               and form["lat"] not in ("21.1458",)     # centroid, not Nagpur
               and form["capacity"] == "24370",        # the network's own median
               json.dumps(form) + f" names_a_place={names_a_place}"
               + "  (shipped pre-filled with 'Nagpur DC' at 21.1458/79.0882 "
               "with a capacity of 5,000; later opened with a blank name that "
               "blocked submission)")

        page.fill("#toolbox-scenario-name", "Greenfield end to end")
        page.select_option("#toolbox-site-city", label="Nagpur")
        page.wait_for_timeout(300)
        page.fill("#toolbox-site-name", "Nagpur DC")
        page.click("#btn-run-toolbox-scenario")
        for _ in range(120):
            page.wait_for_timeout(2000)
            if not page.is_visible("#modal-create-toolbox.visible"):
                break
            if page.query_selector("#scn-creation-error"):
                break
        page.wait_for_timeout(2500)
        err = page.query_selector("#scn-creation-error")
        record("G-08", "The Create Scenario modal runs a greenfield site",
               err is None, err.inner_text() if err else "no error")

        scenarios = page.evaluate("""async () => {
            const d = await import('/js/data.js');
            return d.SCENARIOS.map(s => ({ name: s.name, cost: s.totalCost,
                ref: s.referenceCost, effect: s.changeEffect,
                newSites: (s.newSites || []).length, overrides: s.overrides || [] }));
        }""")
        created = next((s for s in scenarios if s["name"] == "Greenfield end to end"), None)
        record("G-09", "The new site is opened and the scenario has a real effect",
               created is not None and created["newSites"] == 1
               and created["effect"] is not None and abs(created["effect"]) > 1,
               json.dumps(created))

        record("G-10", "The saving is measured against the re-optimised reference",
               created is not None and created["ref"] is not None
               and abs((created["ref"] - created["cost"]) - abs(created["effect"])) < 1,
               f"reference {created['ref']:,.2f} - scenario {created['cost']:,.2f} "
               f"= {created['effect']:,.2f}" if created else "no scenario")
        page.screenshot(path=str(SHOTS / "p107_scenarios.png"), full_page=True)

        # ---- H-12 No invented version string ---------------------------
        page.evaluate("window.openChatbotModal && window.openChatbotModal()")
        page.wait_for_timeout(600)
        page.evaluate("window.askChatbotPrompt && window.askChatbotPrompt('Which DC is most utilised?')")
        page.wait_for_timeout(20000)
        chat = page.evaluate(
            """() => (document.getElementById('chatbot-chat-view') || {}).innerText || ''""")
        record("H-12", "The assistant does not announce a version that does not exist",
               "v2.4" not in chat and "NetGravity" in chat,
               chat.replace("\n", " ")[:200])
        page.screenshot(path=str(SHOTS / "p107_chat.png"))

        real_errors = [e for e in page_errors if "WebGLProgram" not in e
                       and "THREE." not in e]
        record("H-13", "No uncaught page errors",
               not real_errors, json.dumps(real_errors[:3]))

        browser.close()
    server.shutdown()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    engine_checks()
    print()
    browser_checks()

    failed = [c for c in results["checks"] if c["status"] == "FAIL"]
    results["summary"] = {"total": len(results["checks"]),
                          "passed": len(results["checks"]) - len(failed),
                          "failed": len(failed)}
    (OUT / "greenfield_hardcoded_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n{results['summary']['passed']}/{results['summary']['total']} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
