"""Drive the UI with the client's workbook and capture every screen.

Same journey a client would take: create an account, create a project, upload
`NetGravity_Test_Data_Clean.xlsx`, confirm the mapping, then walk the app.
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time
import uuid
from wsgiref.simple_server import make_server

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

WORKBOOK = ROOT.parent / "Dump" / "NetGravity_Test_Data_Clean.xlsx"
SHOTS = pathlib.Path(__file__).parent / "screenshots"
PORT = 5113
BASE = f"http://127.0.0.1:{PORT}/"

TABS = [
    ("nav-item-home", "home"),
    ("nav-item-kpis", "kpis"),
    ("nav-item-twin", "twin"),
    ("nav-item-forecast", "forecast"),
    ("nav-item-scenarios", "scenarios"),
]


def main() -> int:
    # The banner and KPI strip both contain ₹; a Windows console
    # defaults to cp1252 and raises on it, losing the whole run.
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    from app.backend.app import app
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(exist_ok=True)
    server = make_server("127.0.0.1", PORT, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(1.0)

    errors: list = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(
            f"pageerror: {e}\n    stack: {getattr(e, 'stack', '') or ''}"))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate("window.switchAuthPanel('signup')")
        page.wait_for_timeout(400)
        page.fill("#signup-name", "Client Demo")
        page.fill("#signup-email", f"client-{uuid.uuid4().hex[:8]}@example.com")
        page.fill("#signup-password", "Netgravity@2026")
        page.evaluate("window.completeAuth('signup')")
        page.wait_for_selector("#create-project-page:not(.hidden)", timeout=30000)
        page.fill("#proj-name", "Client Sample Network")
        page.click("#proj-create-submit")
        page.wait_for_selector("#upload-data-page:not(.hidden)", timeout=30000)

        page.set_input_files("#ing-file-input", [str(WORKBOOK)])
        page.wait_for_timeout(6000)
        page.screenshot(path=str(SHOTS / "client_01_uploaded.png"))
        page.click("#ing-continue-btn")
        page.wait_for_timeout(9000)
        page.screenshot(path=str(SHOTS / "client_02_mapping.png"))

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
        page.wait_for_timeout(14000)

        for nav_id, name in TABS:
            try:
                page.click(f"#{nav_id}", timeout=15000)
            except Exception as exc:  # noqa: BLE001
                print(f"  {name}: could not open ({str(exc)[:60]})")
                continue
            page.wait_for_timeout(3000)
            page.screenshot(path=str(SHOTS / f"client_{name}.png"))
            print(f"  {name}: captured")

        notice = page.query_selector("#ng-network-notice")
        print("\nBANNER:", (notice.inner_text() if notice else "(none)")[:600])

        strip = page.query_selector("#home-kpi-grid")
        print("HOME KPI STRIP:",
              (strip.inner_text() if strip else "(none)")
              .replace("\n", " | "))

        fbanner = page.query_selector("#home-forecast-banner")
        print("HOME FORECAST:", (fbanner.inner_text() if fbanner else "(none)"))

        # What hydration actually wrote, so an empty field on screen can be
        # told apart from an empty field in the model behind it.
        model = page.evaluate(
            "() => (window.__ngModelProbe ? window.__ngModelProbe() : null)")
        print("MODEL:", model)

        twin = page.evaluate(
            """() => {
                const rows = (sel) => [...document.querySelectorAll(sel + ' tbody tr')]
                  .map(r => r.innerText.replace(/\\s+/g, ' ').trim());
                return {plants: rows('#table-plants'), dcs: rows('#table-dcs'),
                        markets: rows('#table-markets')};
            }"""
        )
        print("\nTWIN plants:", twin["plants"][:4])
        print("TWIN dcs:   ", twin["dcs"][:4])
        print("TWIN mkts:  ", twin["markets"][:4])

        blocking = [e for e in errors if "favicon" not in e.lower()]
        print(f"\nJS errors: {len(blocking)}")
        for e in blocking[:5]:
            print("  ", e[:900])
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
