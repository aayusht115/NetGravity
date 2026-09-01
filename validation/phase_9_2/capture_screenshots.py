"""
Capture high-resolution screenshots of the running NetGravity application for Phase 9.2 Validation.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
SHOTS_DIR.mkdir(parents=True, exist_ok=True)


def capture_all():
    print("Launching Playwright browser automation...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # 1. Landing / Sign-in
        print("Capturing 01_login_project.png...")
        page.goto("http://127.0.0.1:5050/", wait_until="networkidle")
        time.sleep(1)
        page.screenshot(path=str(SHOTS_DIR / "01_login_project.png"), full_page=True)

        # Enter project / app shell
        enter_btn = page.query_selector(".btn-demo, #landing-enter-btn, button:has-text('Select Project'), button:has-text('Demo')")
        if enter_btn:
            enter_btn.click()
            time.sleep(1)

        # If on select project page, click first project
        proj_card = page.query_selector(".proj-card, .select-project-card")
        if proj_card:
            proj_card.click()
            time.sleep(1)

        # 2. Ingestion Flow
        print("Capturing 02_ingestion.png...")
        page.evaluate("if (typeof window.showUploadData === 'function') window.showUploadData();")
        time.sleep(1)
        page.screenshot(path=str(SHOTS_DIR / "02_ingestion.png"), full_page=True)

        # Return to main app shell
        page.evaluate("if (typeof window.enterApp === 'function') window.enterApp();")
        time.sleep(1)

        # 3. Home Cockpit / Dashboard
        print("Capturing 03_dashboard.png...")
        page.evaluate("if (typeof window.navigateToTab === 'function') window.navigateToTab('home');")
        time.sleep(1)
        page.screenshot(path=str(SHOTS_DIR / "03_dashboard.png"), full_page=True)

        # 4. Digital Twin
        print("Capturing 04_digital_twin.png...")
        page.evaluate("if (typeof window.navigateToTab === 'function') window.navigateToTab('twin');")
        time.sleep(1)
        page.screenshot(path=str(SHOTS_DIR / "04_digital_twin.png"), full_page=True)

        # 5. Demand Forecast
        print("Capturing 05_forecast.png...")
        page.evaluate("if (typeof window.navigateToTab === 'function') window.navigateToTab('forecast');")
        time.sleep(1)
        page.screenshot(path=str(SHOTS_DIR / "05_forecast.png"), full_page=True)

        # 6. Scenario Planning
        print("Capturing 06_scenario.png...")
        page.evaluate("if (typeof window.navigateToTab === 'function') window.navigateToTab('scenarios');")
        time.sleep(1)
        page.screenshot(path=str(SHOTS_DIR / "06_scenario.png"), full_page=True)

        # 7. Insight Deep Dive / Reasoning
        print("Capturing 07_insight_reasoning.png...")
        page.evaluate("if (typeof window.navigateToTab === 'function') window.navigateToTab('home');")
        time.sleep(0.5)
        insight_link = page.query_selector(".home2-attention-item, .home-action-btn")
        if insight_link:
            insight_link.click()
            time.sleep(1)
        page.screenshot(path=str(SHOTS_DIR / "07_insight_reasoning.png"), full_page=True)

        # 8. Governance / Decision Log
        print("Capturing 08_governance_decision.png...")
        page.evaluate("if (typeof window.navigateToTab === 'function') window.navigateToTab('governance');")
        time.sleep(1)
        page.screenshot(path=str(SHOTS_DIR / "08_governance_decision.png"), full_page=True)

        browser.close()
    print(f"All screenshots captured successfully in {SHOTS_DIR}")


if __name__ == "__main__":
    capture_all()
