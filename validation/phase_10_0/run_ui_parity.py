"""UI parity check — production app vs the approved standalone.

The standalone (app/standalone/netgravity_standalone.html) is the design
authority. It is opened read-only over file:// and is never modified.

Nothing here is hand-guessed. Every selector is harvested from the standalone's
own markup — each `id`, and each class that carries at least one CSS rule — so
the check covers whatever the design actually contains rather than whatever a
test author remembered to list.

For each selector present in both documents the check compares:

  * COMPUTED STYLE — the properties that decide how a screen looks (display,
    font, colour, spacing, radius, grid/flex tracks, shadow).
  * DIMENSIONS — bounding-box width/height at a fixed viewport. A stylesheet
    that never loads shows up here as a collapsed or full-bleed box.

Both documents are put into the same forced-visible state first, so inactive
screens and closed modals are still measurable and are measured identically.
An element that stays 0x0 in both is reported as NOT_MEASURABLE rather than
counted as a pass, so the pass total is never inflated by elements that simply
never rendered.

Only presentation is compared. The production app deliberately shows real
solver output where the standalone shows demo literals, so text content is
never asserted equal.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
STANDALONE = ROOT / "app" / "standalone" / "netgravity_standalone.html"
PROD_URL = "http://127.0.0.1:5050/"
OUT = pathlib.Path(__file__).parent / "ui_parity_validation.json"
SHOTS = pathlib.Path(__file__).parent / "screenshots"

VIEWPORT = {"width": 1600, "height": 1000}
TOL_PX = 3

PROPS = [
    "width", "height",
    "display", "position", "flex-direction", "justify-content", "align-items",
    "grid-template-columns", "gap", "padding", "margin",
    "font-family", "font-size", "font-weight", "line-height", "letter-spacing",
    "text-transform", "color", "background-color", "background-image",
    "border-width", "border-style", "border-color", "border-radius",
    "box-shadow", "opacity", "overflow", "text-align",
]

# Stylesheets the standalone inlines, in cascade order. Each must be reachable
# in the production document.
SHEETS = [
    "style.css", "landing.css", "auth.css", "home-overview.css", "insights.css",
    "insight-detail.css", "agent-reasoning.css", "chatbot.css", "projects.css",
    "ingestion.css",
]

# Animations must be frozen before anything is measured. A fade-in caught
# mid-flight reports a fractional `opacity`, and an animating ancestor becomes
# the containing block for `position:fixed` children — which silently
# remeasures every modal against its parent instead of the viewport.
REVEAL_JS = """() => {
  document.querySelectorAll('.hidden,[hidden]').forEach(el => {
    el.classList.remove('hidden');
    el.removeAttribute('hidden');
  });
  document.querySelectorAll('[style*="display"]').forEach(el => {
    if (el.style.display === 'none') el.style.display = '';
  });
  const st = document.createElement('style');
  st.textContent = `
    *,*::before,*::after{
      animation: none !important;
      transition: none !important;
      will-change: auto !important;
    }
    .hidden,[hidden]{display:revert !important}
    .modal-overlay{display:flex !important;opacity:1 !important;
                   visibility:visible !important;pointer-events:auto !important}
    .tab-panel{display:block !important;opacity:1 !important}
    .landing-auth-panel{display:block !important}
    #landing-page,#projects-page,#ingestion-page,#app-page{display:block !important}
  `;
  document.head.appendChild(st);
  document.getAnimations().forEach(a => { try { a.cancel(); } catch (e) {} });
}"""

PROBE_ALL_JS = """
(args) => {
  const [selectors, props] = args;
  const out = {};
  const read = (el) => {
    const cs = getComputedStyle(el);
    const r  = el.getBoundingClientRect();
    const s  = {};
    for (const p of props) s[p] = cs.getPropertyValue(p);
    return {w: Math.round(r.width), h: Math.round(r.height), style: s};
  };
  for (const sel of selectors) {
    let els;
    try { els = [...document.querySelectorAll(sel)]; } catch (e) { continue; }
    if (!els.length) { out[sel] = null; continue; }
    out[sel] = {count: els.length, first: read(els[0])};
  }
  return out;
}
"""


def harvest_selectors():
    """Every id and every styled class in the standalone's own markup."""
    text = STANDALONE.read_text(encoding="utf-8")
    lines = text.split("\n")
    css = "\n".join(lines[14:8746])
    body = "\n".join(lines[8844:10161])

    styled = set(re.findall(r"\.([A-Za-z][\w-]*)", css))

    classes = set()
    for m in re.finditer(r'class="([^"]+)"', body):
        for c in m.group(1).split():
            if c in styled:
                classes.add(c)

    ids = set(re.findall(r'id="([A-Za-z][\w-]*)"', body))

    sels = ["#" + i for i in sorted(ids)] + ["." + c for c in sorted(classes)]
    return sels


def main():
    selectors = harvest_selectors()
    results = {"viewport": VIEWPORT, "selectors_harvested": len(selectors),
               "checks": [], "summary": {}}
    SHOTS.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)

        ref = ctx.new_page()
        ref.goto(STANDALONE.as_uri(), wait_until="load")
        ref.wait_for_timeout(2500)
        ref.evaluate(REVEAL_JS)
        ref.wait_for_timeout(400)

        prod = ctx.new_page()
        errors = []
        prod.on("pageerror", lambda e: errors.append(str(e)))
        prod.goto(PROD_URL, wait_until="load")
        prod.wait_for_timeout(2500)
        prod.evaluate(REVEAL_JS)
        prod.wait_for_timeout(400)

        loaded = prod.evaluate(
            """() => [...document.styleSheets]
                 .map(s => (s.href || '').split('/').pop()).filter(Boolean)"""
        )
        order = [s for s in loaded if s in SHEETS]
        for sheet in SHEETS:
            ok = sheet in loaded
            results["checks"].append({
                "check": f"stylesheet loaded: {sheet}", "group": "stylesheets",
                "status": "PASS" if ok else "FAIL",
                "detail": "linked and parsed" if ok else "NOT LOADED",
            })
        results["checks"].append({
            "check": "stylesheet cascade order matches the standalone",
            "group": "stylesheets",
            "status": "PASS" if order == SHEETS else "FAIL",
            "detail": {"expected": SHEETS, "actual": order},
        })

        ref_data = ref.evaluate(PROBE_ALL_JS, [selectors, PROPS])
        prod_data = prod.evaluate(PROBE_ALL_JS, [selectors, PROPS])

        for sel in selectors:
            r, p = ref_data.get(sel), prod_data.get(sel)
            if r is None:
                continue                       # not in the standalone: nothing to match
            if p is None:
                results["checks"].append({
                    "check": sel, "group": "presence", "status": "FAIL",
                    "detail": "in the standalone, MISSING in production",
                })
                continue

            rf, pf = r["first"], p["first"]
            unique = sel.startswith("#") and r["count"] == 1 and p["count"] == 1

            diff = {k: {"standalone": rf["style"][k], "production": pf["style"][k]}
                    for k in PROPS if rf["style"][k] != pf["style"][k]}
            results["checks"].append({
                "check": f"computed style: {sel}", "group": "style",
                "status": "PASS" if not diff else "FAIL",
                "detail": "identical" if not diff else diff,
            })

            # Geometry is only meaningful where the selector names the same one
            # element in both documents. A bare class such as `.flex` matches
            # dozens of unrelated boxes, and "first match" is not the same box
            # once production adds an element above it — so those are compared
            # by match count instead, which is the claim that actually holds.
            if unique:
                if rf["w"] == 0 and rf["h"] == 0 and pf["w"] == 0 and pf["h"] == 0:
                    geom_status = "NOT_MEASURABLE"
                else:
                    dw, dh = abs(rf["w"] - pf["w"]), abs(rf["h"] - pf["h"])
                    geom_status = "PASS" if (dw <= TOL_PX and dh <= TOL_PX) else "FAIL"
                results["checks"].append({
                    "check": f"dimensions: {sel}", "group": "dimensions",
                    "status": geom_status,
                    "detail": f"{rf['w']}x{rf['h']} vs {pf['w']}x{pf['h']}",
                })
            else:
                results["checks"].append({
                    "check": f"element count: {sel}", "group": "counts",
                    "status": "PASS" if r["count"] == p["count"] else "FAIL",
                    "detail": f"standalone {r['count']} vs production {p['count']}",
                })

        results["checks"].append({
            "check": "no blocking JavaScript error in production",
            "group": "runtime",
            "status": "PASS" if not errors else "FAIL",
            "detail": errors[:5] or "none",
        })

        ref.screenshot(path=str(SHOTS / "parity_standalone.png"), full_page=False)
        prod.screenshot(path=str(SHOTS / "parity_production.png"), full_page=False)
        browser.close()

    tally = {}
    for c in results["checks"]:
        tally[c["status"]] = tally.get(c["status"], 0) + 1
    results["summary"] = tally
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")

    fails = [c for c in results["checks"] if c["status"] == "FAIL"]
    for c in fails[:40]:
        print(f"FAIL [{c['group']}] {c['check']}")
        print(f"     {json.dumps(c['detail'], default=str)[:400]}")
    if len(fails) > 40:
        print(f"... and {len(fails) - 40} more failures")

    print(f"\nselectors harvested from the standalone: {len(selectors)}")
    print(" · ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
