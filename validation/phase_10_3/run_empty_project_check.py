"""What the app shows a user who has NOT uploaded anything.

Every other harness uploads the workbook first, which is why this state went
unchecked: a project with no bound network rendered the prototype's own
network on every screen — ₹12.8L total cost, 94% utilisation, five facilities
in the picker, two solved scenarios, a costed recommendation — under a banner
that read "This project has no network yet".

The client-side model now starts empty and is reset whenever a project has
nothing bound, so each screen shows its own empty state. These checks pin that:
an absence must render as an absence, not as somebody else's network.
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

OUT = pathlib.Path(__file__).parent
SHOTS = OUT / "screenshots"
PORT = 5137
BASE = f"http://127.0.0.1:{PORT}/"

#: Anything from the prototype network. None of it may appear on any screen.
DEMO_MARKERS = (
    "Baddi", "Guwahati", "Delhi NCR DC", "Lucknow", "DC_DELHI", "PLT_BADDI",
    "12.8L", "12.85L", "89.5%", "94%", "7.8%", "96.7%",
    "DC_CENTRAL", "DC_EAST", "DC_NORTH_NEW", "DC_SOUTH_NEW",
)

results = {"checks": []}


def record(cid: str, name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results["checks"].append(
        {"id": cid, "name": name, "status": status, "detail": detail}
    )
    print(f"[{status:4}] {cid:6} {name}" + (f" — {detail}" if detail else ""))


def leaked(text: str) -> list:
    return [m for m in DEMO_MARKERS if m in (text or "")]


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
        page.fill("#signup-name", "Empty Project")
        page.fill("#signup-email", f"empty-{uuid.uuid4().hex[:8]}@kearney.com")
        page.fill("#signup-password", "Netgravity@2026")
        page.fill("#signup-password-confirm", "Netgravity@2026")
        page.evaluate("window.completeAuth('signup')")
        page.wait_for_selector("#create-project-page:not(.hidden)", timeout=30000)
        page.fill("#proj-name", "Nothing Uploaded")
        page.click("#proj-create-submit")
        page.wait_for_selector("#upload-data-page:not(.hidden)", timeout=30000)

        # Take the "Skip for now" route into the shell, with nothing bound.
        page.click("#ing-skip-btn")
        page.wait_for_timeout(7000)
        page.screenshot(path=str(SHOTS / "empty_01_home.png"), full_page=True)

        def inner(sel: str) -> str:
            el = page.query_selector(sel)
            return el.inner_text().replace("\n", " | ") if el else ""

        home = inner("#home-kpi-grid")
        record("E-01", "Home KPI strip reports no result rather than a number",
               "—" in home and not leaked(home), home[:180])

        picker = page.evaluate(
            """() => {
                const s = document.getElementById('sel-facility');
                return s ? [...s.options].map(o => o.value + '=' + o.text) : [];
            }"""
        )
        record("E-02", "Facility picker offers no prototype facility",
               not leaked(json.dumps(picker)), json.dumps(picker))

        # ---- Digital Twin -------------------------------------------
        page.click("#nav-item-twin")
        page.wait_for_timeout(2500)
        btn = page.query_selector("#twin-view-toggle .toggle-btn[data-view='2d']")
        if btn:
            btn.click()
        page.wait_for_timeout(2500)
        page.screenshot(path=str(SHOTS / "empty_02_twin.png"), full_page=True)

        drawn = page.evaluate(
            """() => {
                const h = document.getElementById('map-twin');
                return h ? h.querySelectorAll('.leaflet-marker-icon').length : -1;
            }"""
        )
        record("E-03", "The 2D map draws no facility", drawn == 0, f"{drawn} markers")

        tables = page.evaluate(
            """() => {
                const rows = (s) => [...document.querySelectorAll(s + ' tbody tr')]
                    .map(r => r.innerText.replace(/\\s+/g, ' ').trim());
                return { plants: rows('#table-plants'), dcs: rows('#table-dcs'),
                         markets: rows('#table-markets') };
            }"""
        )
        record("E-04", "Twin tables list no prototype facility",
               not leaked(json.dumps(tables)), json.dumps(tables)[:200])

        # ---- KPI screen ----------------------------------------------
        page.click("#nav-item-kpis")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(SHOTS / "empty_03_kpis.png"), full_page=True)
        kpis = inner("#dash-metrics-grid")
        record("E-05", "The KPI screen states it has no facility",
               "No facility" in kpis and not leaked(kpis), kpis[:180])

        # ---- Forecast -------------------------------------------------
        page.click("#nav-item-forecast")
        page.wait_for_timeout(2500)
        fc = page.evaluate(
            """() => {
                const t = (id) => (document.getElementById(id) || {}).textContent || '';
                return { model: t('fc-model'), series: t('fc-series'),
                         periods: t('fc-periods') };
            }"""
        )
        record("E-06", "The forecast card reports no forecast",
               all(v.strip() in ("—", "") for v in fc.values()), json.dumps(fc))

        # ---- Scenarios ------------------------------------------------
        page.click("#nav-item-scenarios")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(SHOTS / "empty_04_scenarios.png"), full_page=True)
        scn = inner("#tab-scenarios")
        record("E-07", "Scenario screen shows no prototype scenario",
               not leaked(scn), json.dumps(leaked(scn)))
        # The screen must SAY it has nothing, and show no figures.
        #
        # This asserted the literal string "No solved scenarios", which was the
        # one empty-state sentence the page had — it now distinguishes three
        # different empty states (no solved network, no scenario selected, no
        # recommendation to give), each saying which one applies. The check
        # follows the intent rather than the old wording, and additionally
        # requires that no currency figure appears, which it did not before.
        says_empty = any(phrase in scn for phrase in (
            "has not been solved",
            "No scenario yet",
            "No scenario selected",
            "no scenario has been solved",
        ))
        record("E-08", "Scenario screen says it has nothing, and shows no figures",
               says_empty and "₹" not in scn, scn[:200])

        # ---- Assistant ------------------------------------------------
        page.click("#nav-item-home")
        page.wait_for_timeout(1500)
        page.evaluate("window.openChatbotModal && window.openChatbotModal()")
        page.wait_for_timeout(600)
        page.evaluate(
            "window.askChatbotPrompt && window.askChatbotPrompt("
            "'what is my total network cost?')"
        )
        page.wait_for_timeout(8000)
        page.screenshot(path=str(SHOTS / "empty_05_chat.png"), full_page=True)
        chat = page.evaluate(
            """() => {
                const b = [...document.querySelectorAll('.chat-bubble-ai')];
                return b.length ? b[b.length - 1].innerText.replace(/\\s+/g, ' ') : '';
            }"""
        )
        record("E-09", "The assistant refuses rather than answering about another network",
               "no analysed network" in chat and not leaked(chat), chat[:200])

        record("E-10", "No uncaught page errors",
               not page_errors, "; ".join(page_errors[:3])[:300])

        browser.close()

    (OUT / "empty_project_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    print(f"\n{passed} passed, {failed} failed of {len(results['checks'])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
