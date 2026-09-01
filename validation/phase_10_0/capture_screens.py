"""Capture each app screen from the production app and from the standalone.

Both are driven by real navigation — sign in, then click the sidebar — rather
than by forcing CSS, so what is captured is what a user actually sees. The
production app is taken through the full ingestion flow first, because its
screens render the uploaded network rather than demo literals.

Writes screenshots/screen_<tab>_<production|standalone>.png.
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

OUT = pathlib.Path(__file__).parent
SHOTS = OUT / "screenshots"
STANDALONE = (ROOT / "app" / "standalone" / "netgravity_standalone.html").as_uri()
PORT = 5103
BASE = f"http://127.0.0.1:{PORT}/"
VIEWPORT = {"width": 1440, "height": 900}

TABS = [
    ("nav-item-home", "home"),
    ("nav-item-kpis", "kpis"),
    ("nav-item-twin", "twin"),
    ("nav-item-forecast", "forecast"),
    ("nav-item-scenarios", "scenarios"),
]

# The same fixtures run_prototype_e2e.py uses, so these screenshots show the
# state that test asserts against.
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


def settle(page):
    """Wait out the ingestion loading overlay, which swallows nav clicks."""
    for _ in range(60):
        busy = page.evaluate(
            """() => {
                const o = document.getElementById('loading-modal-overlay');
                return !!(o && o.classList.contains('active'));
            }"""
        )
        if not busy:
            return
        page.wait_for_timeout(500)


def shoot_tabs(page, tag):
    settle(page)
    for nav_id, name in TABS:
        try:
            page.click(f"#{nav_id}", timeout=15000)
        except Exception as exc:                       # pragma: no cover - diagnostic
            print(f"  {tag}/{name}: could not open ({exc})")
            continue
        page.wait_for_timeout(2200)
        page.screenshot(path=str(SHOTS / f"screen_{name}_{tag}.png"))
        print(f"  {tag}/{name}: captured")


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

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT)

        # ── Standalone: sign in straight into the demo shell ──────────
        ref = ctx.new_page()
        ref.goto(STANDALONE, wait_until="load")
        ref.wait_for_timeout(2500)
        # enterApp() opens the shell directly; completeAuth() would stop at
        # project selection, which the demo build has no server to populate.
        ref.evaluate("window.enterApp && window.enterApp()")
        ref.wait_for_timeout(3000)
        print("standalone:")
        shoot_tabs(ref, "standalone")

        # ── Production: real account, project and upload ──────────────
        prod = ctx.new_page()
        prod.goto(BASE, wait_until="networkidle", timeout=60000)
        prod.wait_for_timeout(1500)
        prod.evaluate("window.switchAuthPanel('signup')")
        prod.wait_for_timeout(400)
        prod.fill("#signup-name", "Screen Capture")
        prod.fill("#signup-email", f"shots-{uuid.uuid4().hex[:8]}@example.com")
        prod.fill("#signup-password", "Netgravity@2026")
        prod.evaluate("window.completeAuth('signup')")
        prod.wait_for_selector("#create-project-page:not(.hidden)", timeout=30000)
        prod.fill("#proj-name", "Screenshot Network")
        prod.click("#proj-create-submit")
        prod.wait_for_selector("#upload-data-page:not(.hidden)", timeout=30000)
        prod.set_input_files("#ing-file-input", paths)
        prod.wait_for_timeout(4000)
        prod.click("#ing-continue-btn")
        prod.wait_for_timeout(9000)
        # Same walk the E2E uses — the mapping flow has several confirm steps
        # and their ids differ per stage.
        for _ in range(12):
            clicked = False
            for sel in ("#ing-confirm-mapping-btn", "#ing-pdf-continue-btn",
                        "#ing-finish-btn", "#ing-build-btn"):
                btn = prod.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    clicked = True
                    prod.wait_for_timeout(3500)
                    break
            if not clicked:
                break
            if prod.is_visible(".app-shell") and not prod.is_visible("#ingestion-page"):
                break
        prod.wait_for_timeout(9000)
        print("production:")
        shoot_tabs(prod, "production")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
