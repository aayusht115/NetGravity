"""Drive the whole client journey in a browser and check every screen.

Phase 10.2 verified the API. These checks verify what a user actually SEES:
the upload and mapping-review screens, the 2D Digital Twin, the KPI screen and
the assistant — each driven with `Dump/NetGravity_Test_Data_Clean.xlsx`.

Every check asserts on rendered DOM, not on a JSON payload, because each of the
defects this phase fixed was invisible to an API test: the mapping table showed
nine invented rows while the parser returned fifty real ones, the 2D map drew
nothing while the network was fully loaded, the KPI screen returned early on a
facility id that no longer existed, and the assistant read a response field the
API does not emit.
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
PORT = 5121
BASE = f"http://127.0.0.1:{PORT}/"

results = {"checks": []}


def record(cid: str, name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results["checks"].append(
        {"id": cid, "name": name, "status": status, "detail": detail}
    )
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
        page.fill("#signup-name", "Flow Check")
        page.fill("#signup-email", f"flow-{uuid.uuid4().hex[:8]}@kearney.com")
        page.fill("#signup-password", "Netgravity@2026")
        page.fill("#signup-password-confirm", "Netgravity@2026")
        page.evaluate("window.completeAuth('signup')")
        page.wait_for_selector("#create-project-page:not(.hidden)", timeout=30000)
        page.fill("#proj-name", "Client Sample Network")
        page.click("#proj-create-submit")
        page.wait_for_selector("#upload-data-page:not(.hidden)", timeout=30000)

        # ---- Upload screen ------------------------------------------
        page.set_input_files("#ing-file-input", [str(WORKBOOK)])
        page.wait_for_timeout(6000)
        page.screenshot(path=str(SHOTS / "flow_01_upload.png"), full_page=True)

        size_text = page.inner_text("#ing-file-table-slot")
        record(
            "U-01", "Upload table shows a real file size",
            "KB" in size_text and "0.0 MB" not in size_text,
            [ln for ln in size_text.splitlines() if "KB" in ln or "MB" in ln][:1],
        )

        # ---- Mapping review screen ----------------------------------
        page.click("#ing-continue-btn")
        page.wait_for_timeout(9000)
        page.wait_for_selector("#ing-map-table-slot", timeout=30000)
        page.screenshot(path=str(SHOTS / "flow_02_mapping.png"), full_page=True)

        mapping = page.evaluate(
            """() => {
                const rows = [...document.querySelectorAll('#ing-map-table-slot tbody tr')];
                return rows.map(r => ({
                  source: r.cells[0]?.innerText.trim(),
                  sample: r.cells[1]?.innerText.trim(),
                  mapped: r.querySelector('select')?.value,
                  status: r.cells[4]?.innerText.trim(),
                }));
            }"""
        )
        sources = [m["source"] for m in mapping]
        record(
            "U-02", "Mapping lists the workbook's own columns",
            len(mapping) == 51 and "Facility_ID" in sources and "Rate_Per_Unit" in sources,
            f"{len(mapping)} rows; first={sources[:3]}",
        )
        demo_cols = {"customer_id", "origin_dc", "destination_market", "amt_rs", "misc_ref"}
        record(
            "U-03", "No prototype mapping rows survive",
            not demo_cols.intersection(sources),
            f"demo columns present: {sorted(demo_cols.intersection(sources))}",
        )
        by_source = {m["source"]: m for m in mapping}
        expected = {
            "Facility_ID": "Facility ID",
            "Capacity_Units": None,       # differs by sheet; checked below
            "Rate_Per_Unit": "Freight rate",
            "Latitude": "Latitude",
            "Demand_Units": "Demand quantity",
            "Service_SLA_Days": "Service SLA (days)",
            "Unit_Cost": "Unit value",
        }
        wrong = {
            col: by_source.get(col, {}).get("mapped")
            for col, want in expected.items()
            if want and by_source.get(col, {}).get("mapped") != want
        }
        record("U-04", "Each column maps to the field the parser reads",
               not wrong, json.dumps(wrong))

        cap_values = {m["mapped"] for m in mapping if m["source"] == "Capacity_Units"}
        record(
            "U-05", "The same column name resolves per sheet",
            cap_values == {"Facility capacity", "Lane capacity"},
            f"Capacity_Units -> {sorted(cap_values)}",
        )
        unused = [m["source"] for m in mapping if "Not used" in (m["status"] or "")]
        record(
            "U-06", "Only genuinely unused columns are marked unused",
            unused == ["Product_Category"], f"unused={unused}",
        )

        # ---- Layout: nothing may overflow the page ------------------
        overflow = page.evaluate(
            """() => {
                const doc = document.documentElement;
                const offenders = [];
                document.querySelectorAll('#ingestion-page *').forEach(el => {
                  const r = el.getBoundingClientRect();
                  if (r.width > 0 && r.right > doc.clientWidth + 1) {
                    offenders.push(el.className || el.tagName);
                  }
                });
                return {
                  pageScrollsX: doc.scrollWidth > doc.clientWidth + 1,
                  offenders: [...new Set(offenders)].slice(0, 8),
                };
            }"""
        )
        record("U-07", "Nothing on the review screen overflows the viewport",
               not overflow["pageScrollsX"] and not overflow["offenders"],
               json.dumps(overflow))

        wrapped = page.evaluate(
            """() => {
                const el = [...document.querySelectorAll('.ing-map-sample')]
                  .sort((a,b) => b.innerText.length - a.innerText.length)[0];
                if (!el) return null;
                const cs = getComputedStyle(el);
                return { chars: el.innerText.length, whiteSpace: cs.whiteSpace,
                         display: cs.display,
                         withinCell: el.getBoundingClientRect().width
                                     <= el.parentElement.getBoundingClientRect().width + 1 };
            }"""
        )
        record("U-08", "Long sample values wrap inside their cell",
               bool(wrapped) and wrapped["whiteSpace"] == "normal" and wrapped["withinCell"],
               json.dumps(wrapped))

        confirm = page.evaluate(
            """() => {
                const b = document.getElementById('ing-confirm-mapping-btn');
                if (!b) return null;
                const r = b.getBoundingClientRect();
                return { visible: r.width > 0 && r.right <= document.documentElement.clientWidth + 1,
                         disabled: b.disabled };
            }"""
        )
        record("U-09", "The confirm control is reachable on screen",
               bool(confirm) and confirm["visible"] and not confirm["disabled"],
               json.dumps(confirm))

        rows_label = page.inner_text(".ing-file-summary-card")
        record("U-10", "Rows read is the file's real row count",
               "927" in rows_label, rows_label.replace("\n", " | ")[:160])

        # ---- Into the app -------------------------------------------
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

        # ---- Digital Twin: 2D map -----------------------------------
        page.click("#nav-item-twin")
        page.wait_for_timeout(2500)
        btn2d = page.query_selector("#twin-view-toggle .toggle-btn[data-view='2d']")
        if btn2d:
            btn2d.click()
        page.wait_for_timeout(3000)
        page.screenshot(path=str(SHOTS / "flow_03_twin2d.png"), full_page=True)

        markers = page.evaluate(
            """() => {
                const host = document.getElementById('map-twin');
                if (!host) return { host: false };
                return {
                  host: true,
                  markers: host.querySelectorAll('.leaflet-marker-icon, path.leaflet-interactive').length,
                  tiles: host.querySelectorAll('img.leaflet-tile').length,
                };
            }"""
        )
        # 15 nodes + 15 solved corridors, drawn as SVG paths and marker icons.
        record("U-11", "The 2D twin draws the uploaded network without a toggle click",
               bool(markers.get("host")) and markers.get("markers", 0) >= 15,
               json.dumps(markers))

        names = page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('#map-twin .leaflet-marker-icon').forEach(m => {
                  out.push((m.getAttribute('title') || m.innerText || '').trim());
                });
                return out.filter(Boolean).slice(0, 6);
            }"""
        )
        record("U-12", "Map markers are the client's own facilities",
               not any("Baddi" in n or "DC Delhi NCR" in n for n in names),
               json.dumps(names))

        # ---- KPI screen ---------------------------------------------
        page.click("#nav-item-kpis")
        page.wait_for_timeout(3000)
        page.screenshot(path=str(SHOTS / "flow_04_kpis.png"), full_page=True)

        # The facility's identity on this screen is the topbar selector — the
        # `dash-facility-name`/`dash-facility-type` elements the renderer also
        # writes to are in neither template.
        kpi = page.evaluate(
            """() => {
                const grid = document.getElementById('dash-metrics-grid');
                const sel = document.getElementById('sel-facility');
                const lanes = document.querySelectorAll('#table-dash-lanes tbody tr');
                const opts = sel ? [...sel.options].map(o => ({ v: o.value, t: o.text })) : [];
                return {
                  cards: grid ? grid.children.length : 0,
                  text: grid ? grid.innerText.replace(/\\s+/g, ' ').slice(0, 200) : '',
                  selected: sel ? sel.value : '',
                  selectedText: sel && sel.selectedIndex >= 0
                    ? sel.options[sel.selectedIndex].text : '',
                  options: opts,
                  laneRows: lanes.length,
                };
            }"""
        )
        record("U-13", "The KPI screen renders cards and corridors",
               kpi["cards"] >= 6 and kpi["laneRows"] > 0,
               f"{kpi['cards']} cards, {kpi['laneRows']} corridor rows")

        demo_ids = {"DC_DELHI", "DC_MUMBAI", "DC_BENGALURU", "DC_KOLKATA", "DC_GUWAHATI"}
        offered = {o["v"] for o in kpi["options"]}
        record("U-14", "The facility selector offers this network's facilities",
               bool(offered) and not demo_ids.intersection(offered),
               json.dumps(kpi["options"]))

        # The DC/plant distinction now comes from the loaded arrays, not from
        # an id prefix. F004-F008 are DCs and would have failed the old
        # `id.startsWith('DC_')` test.
        role = page.evaluate(
            """() => {
                const sel = document.getElementById('sel-facility');
                return window.__ngFacilityRole
                  ? window.__ngFacilityRole(sel ? sel.value : null) : null;
            }"""
        )
        record("U-15", "A distribution centre is recognised as one",
               role == "DC", f"{kpi['selected']} -> {role}")

        fabricated = [s for s in ("1.8% vs last period", "3.2% vs budget",
                                  "14.8t", "0.42 kg", "140%", "3.4L")
                      if s in kpi["text"]]
        record("U-19", "No fabricated deltas remain on the KPI cards",
               not fabricated, json.dumps(fabricated))

        # ---- Forecast -----------------------------------------------
        page.click("#nav-item-forecast")
        page.wait_for_timeout(3500)
        page.screenshot(path=str(SHOTS / "flow_06_forecast.png"), full_page=True)
        fc = page.evaluate(
            """() => {
                const t = (id) => (document.getElementById(id) || {}).textContent || '';
                return { model: t('fc-model').trim(), series: t('fc-series').trim(),
                         periods: t('fc-periods').trim(),
                         accuracy: t('fc-accuracy').trim(),
                         count: t('fc-series-count').trim() };
            }"""
        )
        record("U-20", "The forecast card reports the engine's own run",
               "QuantileRegression" in fc["model"] and "36" in fc["periods"],
               json.dumps(fc))
        record("U-21", "No prototype forecast copy survives",
               "North India" not in json.dumps(fc)
               and "Enhanced Demand Forecast" not in fc["model"],
               fc["model"])

        # ---- Scenarios ----------------------------------------------
        page.click("#nav-item-scenarios")
        page.wait_for_timeout(3500)
        page.screenshot(path=str(SHOTS / "flow_07_scenarios.png"), full_page=True)
        scn = page.evaluate(
            """() => {
                const panel = document.getElementById('tab-scenarios');
                const txt = panel ? panel.innerText.replace(/\\s+/g, ' ') : '';
                return { chars: txt.length, text: txt.slice(0, 220) };
            }"""
        )
        demo_scn = ("Baddi", "Guwahati", "SCN_REBALANCE", "Lucknow")
        record("U-22", "Scenario screen shows no prototype scenarios",
               not any(d in scn["text"] for d in demo_scn), scn["text"][:180])
        record("U-23", "Scenario screen says it has nothing rather than nothing at all",
               scn["chars"] > 40, f"{scn['chars']} chars rendered")

        # ---- Home ----------------------------------------------------
        page.click("#nav-item-home")
        page.wait_for_timeout(3000)
        page.screenshot(path=str(SHOTS / "flow_08_home.png"), full_page=True)
        home = page.evaluate(
            """() => {
                const g = document.getElementById('home-kpi-grid');
                return { text: g ? g.innerText.replace(/\\s+/g, ' ') : '' };
            }"""
        )
        record("U-24", "Home KPI strip carries the uploaded network's cost",
               "180.7L" in home["text"] or "\u20b9180" in home["text"],
               home["text"][:180])
        record("U-25", "Home strip shows no null or demo values",
               "null" not in home["text"] and "12.8L" not in home["text"],
               home["text"][:180])

        # ---- Assistant ----------------------------------------------
        page.evaluate("window.openChatbotModal && window.openChatbotModal()")
        page.wait_for_timeout(600)
        page.evaluate(
            "window.askChatbotPrompt && window.askChatbotPrompt("
            "'what is the current network state?')"
        )
        page.wait_for_timeout(60000)
        page.screenshot(path=str(SHOTS / "flow_05_chat.png"), full_page=True)

        chat = page.evaluate(
            """() => {
                const bubbles = [...document.querySelectorAll('.chat-bubble-ai')];
                const last = bubbles[bubbles.length - 1];
                const badge = last ? last.querySelector('.ai-badge-chip') : null;
                return {
                  bubbles: bubbles.length,
                  topic: badge ? badge.innerText.trim() : '',
                  text: last ? last.innerText.replace(/\\s+/g, ' ').trim() : '',
                };
            }"""
        )
        # A timeout or an unreachable engine must FAIL this, not pass it: the
        # first version only excluded the "did not return an answer" wording,
        # so an aborted request satisfied it.
        bad = ("did not return an answer", "could not reach the analysis engine",
               "timed out", "NO NETWORK LOADED")
        answered = bool(chat["text"]) and not any(b in chat["text"] for b in bad)
        record("U-16", "The assistant returns a grounded answer",
               answered, chat["text"][:200])
        record("U-17", "The answer is about the user's network, not the demo one",
               answered and not any(x in chat["text"] for x in
                       ("DC_CENTRAL", "DC_EAST", "DC_NORTH_NEW", "DC_SOUTH_NEW")),
               chat["text"][:160])

        record("U-18", "No uncaught page errors on the journey",
               not page_errors, "; ".join(page_errors[:3])[:300])

        browser.close()

    (OUT / "ui_flow_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    print(f"\n{passed} passed, {failed} failed of {len(results['checks'])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
