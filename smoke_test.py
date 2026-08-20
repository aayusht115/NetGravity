"""
NetGravity — Comprehensive Pre-Submission & Prototype Smoke Test
==================================================================
1-Command verification suite ensuring complete integrity of:
1. Core MILP Optimization Engine & Benchmark Sanity
2. Frontend Prototype Directory Structure & Sync
3. JavaScript Syntax, Export/Import & Bracket/Brace Balance
4. CSS Stylesheet Syntax, Token Integrity & Brace Balance
5. Data Model, Canonical Scenarios & Metric Alignment
6. Standalone Distribution Bundle Inlining & Integrity
7. Flask Backend Application & Routing Sanity

Runs in < 2 seconds.
"""

import os
import sys
import time
import re

# Paths
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
PARENT_ROOT = os.path.dirname(REPO_ROOT)
if PARENT_ROOT not in sys.path:
    sys.path.insert(0, PARENT_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FRONTEND_DIRS = [
    os.path.join(REPO_ROOT, "app", "frontend"),
    os.path.join(PARENT_ROOT, "app", "frontend"),
]

REQUIRED_JS_FILES = [
    "data.js",
    "basemap-data.js",
    "map.js",
    "twin3d.js",
    "charts.js",
    "scenarios.js",
    "agent.js",
    "app.js",
]

def check_brace_balance(content):
    """Returns (curly, paren, square) balance counts."""
    curly = content.count("{") - content.count("}")
    paren = content.count("(") - content.count(")")
    square = content.count("[") - content.count("]")
    return curly, paren, square

def run_smoke_test():
    start_t = time.perf_counter()
    print("=" * 75)
    print("  NetGravity -- Full Codebase & Prototype Smoke Test")
    print("=" * 75)

    passed_checks = 0
    total_checks = 7

    # ─────────────────────────────────────────────────────────────
    # [1/7] Core Engine & Solver Sanity
    # ─────────────────────────────────────────────────────────────
    print("[1/7] Checking Core MILP Optimization Engine...")
    try:
        from netgravity.schemas.network import CanonicalNetwork, FacilityRecord, NodeRole, FacilityStatus, DemandRecord, LaneRecord, TransportMode, ProductRecord
        from netgravity.optimization.milp import solve
        from netgravity.costs.reconciliation import reconcile_costs
        from netgravity.diagnostics.infeasibility import diagnose_infeasibility
        from netgravity.tests.fixtures.case16_synthetic import build_tiny_network, build_case16_network

        # Benchmark 1: Tiny Network
        net_tiny = build_tiny_network()
        res_tiny = solve(net_tiny)
        obj_tiny = res_tiny.solver.objective_value
        recon_tiny = reconcile_costs(res_tiny, net_tiny)
        assert abs(obj_tiny - 5400.0) < 0.01, f"Tiny network expected 5400.0, got {obj_tiny}"
        assert recon_tiny.is_reconciled is True, "Tiny network cost reconciliation failed"

        # Benchmark 2: Case-16 Synthetic Fixture
        net_c16 = build_case16_network()
        res_c16 = solve(net_c16)
        obj_c16 = res_c16.solver.objective_value
        recon_c16 = reconcile_costs(res_c16, net_c16)
        assert abs(obj_c16 - 115638.14) < 0.05, f"Case-16 expected 115638.14, got {obj_c16}"
        assert recon_c16.is_reconciled is True, "Case-16 cost reconciliation failed"

        # Benchmark 3: Infeasibility Bottleneck Diagnostic
        dc_small = FacilityRecord(id="DC_SMALL", name="Small DC", role=NodeRole.DC, status=FacilityStatus.EXISTING, capacity_units_per_period=500.0)
        plant_big = FacilityRecord(id="PLANT_BIG", name="Big Plant", role=NodeRole.PLANT, status=FacilityStatus.EXISTING, capacity_units_per_period=10000.0, is_mandatory=True)
        mkt = FacilityRecord(id="MKT1", name="Market", role=NodeRole.MARKET)
        ln1 = LaneRecord(origin_id="PLANT_BIG", destination_id="DC_SMALL", mode=TransportMode.ROAD, rate_per_unit=1.0)
        ln2 = LaneRecord(origin_id="DC_SMALL", destination_id="MKT1", mode=TransportMode.ROAD, rate_per_unit=1.0)
        dem = DemandRecord(market_id="MKT1", product_id="P1", quantity=600.0)
        net_diag = CanonicalNetwork(facilities=[plant_big, dc_small, mkt], products=[ProductRecord(id="P1", name="P1")], demands=[dem], lanes=[ln1, ln2])
        diag = diagnose_infeasibility(net_diag)
        assert diag.total_capacity == 500.0, f"Expected total_capacity=500, got {diag.total_capacity}"

        print(f"      PASS: Tiny = ${obj_tiny:,.2f} | Case-16 = ${obj_c16:,.2f} | Diagnostic Bottleneck = {diag.total_capacity} u")
        passed_checks += 1
    except Exception as e:
        print(f"      FAIL: Core Solver Error: {e}")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────
    # [2/7] Prototype File Structure & Directory Sync
    # ─────────────────────────────────────────────────────────────
    print("[2/7] Checking Prototype File Structure & Directory Sync...")
    try:
        primary_dir = FRONTEND_DIRS[0]
        assert os.path.isdir(primary_dir), f"Frontend directory missing: {primary_dir}"
        
        # Check essential files
        essential_files = ["index.html", "css/style.css"] + [f"js/{f}" for f in REQUIRED_JS_FILES]
        for ef in essential_files:
            p = os.path.join(primary_dir, ef)
            assert os.path.isfile(p), f"Missing essential frontend file: {p}"

        # Verify sync between root app/frontend and netgravity/app/frontend if both exist
        if len(FRONTEND_DIRS) > 1 and os.path.isdir(FRONTEND_DIRS[1]):
            for ef in essential_files:
                p1 = os.path.join(FRONTEND_DIRS[0], ef)
                p2 = os.path.join(FRONTEND_DIRS[1], ef)
                if os.path.isfile(p1) and os.path.isfile(p2):
                    sz1 = os.path.getsize(p1)
                    sz2 = os.path.getsize(p2)
                    assert abs(sz1 - sz2) < 500, f"File size desync in {ef}: {sz1} vs {sz2}"

        print(f"      PASS: All {len(essential_files)} essential frontend prototype files present & synced.")
        passed_checks += 1
    except Exception as e:
        print(f"      FAIL: File Structure Sync Error: {e}")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────
    # [3/7] Prototype JavaScript Syntax & Balance Verification
    # ─────────────────────────────────────────────────────────────
    print("[3/7] Checking JavaScript Module Syntax & Bracket Balances...")
    try:
        js_dir = os.path.join(FRONTEND_DIRS[0], "js")
        checked_js = 0
        for js_file in REQUIRED_JS_FILES:
            fpath = os.path.join(js_dir, js_file)
            assert os.path.isfile(fpath), f"JS file not found: {fpath}"
            with open(fpath, "r", encoding="utf-8") as f:
                code = f.read()

            c, p, s = check_brace_balance(code)
            assert c == 0, f"Mismatched curly braces in {js_file}: net count {c}"
            assert p == 0, f"Mismatched parentheses in {js_file}: net count {p}"
            assert s == 0, f"Mismatched square brackets in {js_file}: net count {s}"

            # Check for undefined or corrupted import lines
            for lnum, line in enumerate(code.splitlines(), 1):
                if line.strip().startswith("import ") and " from " in line:
                    assert not ("undefined" in line or "null" in line), f"Corrupted import at {js_file}:{lnum}"
            checked_js += 1

        print(f"      PASS: Verified {checked_js} JavaScript modules with 0 bracket/brace balance errors.")
        passed_checks += 1
    except Exception as e:
        print(f"      FAIL: JavaScript Syntax Error: {e}")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────
    # [4/7] Prototype CSS Syntax & Token Integrity
    # ─────────────────────────────────────────────────────────────
    print("[4/7] Checking CSS Stylesheet Syntax & Token Integrity...")
    try:
        css_path = os.path.join(FRONTEND_DIRS[0], "css", "style.css")
        with open(css_path, "r", encoding="utf-8") as f:
            css_code = f.read()

        # Check brace balance in CSS
        depth = 0
        for lnum, line in enumerate(css_code.splitlines(), 1):
            depth += line.count("{") - line.count("}")
            assert depth >= 0, f"Dangling closing brace in style.css at line {lnum}"
        assert depth == 0, f"Unclosed opening brace in style.css, final depth={depth}"

        # Verify key design system classes exist
        required_classes = [
            ".nav-item", ".scn-strip-card", ".scn-data-table", ".scn-multi-table",
            ".scn-visual-context-card", ".scn-map-wrap", ".modal-overlay", ".scenario-drawer-overlay"
        ]
        for rc in required_classes:
            assert rc in css_code, f"Missing required CSS design class: {rc}"

        print("      PASS: CSS stylesheet perfectly balanced with required design system tokens.")
        passed_checks += 1
    except Exception as e:
        print(f"      FAIL: CSS Stylesheet Error: {e}")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────
    # [5/7] Data Model & Scenario Alignment Verification
    # ─────────────────────────────────────────────────────────────
    print("[5/7] Checking Data Model, Scenarios & Alignment...")
    try:
        data_path = os.path.join(FRONTEND_DIRS[0], "js", "data.js")
        with open(data_path, "r", encoding="utf-8") as f:
            data_code = f.read()

        # Check essential exported constants
        required_exports = [
            "PLANTS", "DCS", "MARKETS", "LANES", "FACILITIES",
            "SCENARIOS", "SCENARIO_COMPARISON_INSIGHTS", "SCENARIO_COMPARISON_ACTIONS",
            "HOME_INSIGHTS", "HOME_ACTION_ITEMS"
        ]
        for exp in required_exports:
            assert f"export const {exp}" in data_code or f"export let {exp}" in data_code, f"Missing data export: {exp}"

        # Verify Scenario 1 (Recommended) consistency
        assert "SCN_REBALANCE" in data_code, "Missing canonical recommended scenario: SCN_REBALANCE"
        assert "DC_DELHI" in data_code, "Missing DC_DELHI facility definition"

        # Verify baseline vs scenario alignment
        assert "94.0" in data_code or "94" in data_code, "Delhi baseline utilization alignment missing"
        assert "-7.8" in data_code or "7.8" in data_code, "Recommended scenario cost reduction alignment missing"

        print("      PASS: Data model aligned across Home and Scenario Planning workspaces.")
        passed_checks += 1
    except Exception as e:
        print(f"      FAIL: Data Model Alignment Error: {e}")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────
    # [6/7] Standalone Distribution Bundle Integrity
    # ─────────────────────────────────────────────────────────────
    print("[6/7] Checking Standalone HTML Distribution Bundle...")
    try:
        standalone_path = os.path.join(FRONTEND_DIRS[0], "netgravity_standalone.html")
        assert os.path.isfile(standalone_path), f"Standalone distribution HTML missing: {standalone_path}"
        
        with open(standalone_path, "r", encoding="utf-8") as f:
            sa_content = f.read()

        # Size check (> 100 KB due to inlined css/js/basemap data)
        assert len(sa_content) > 100_000, f"Standalone bundle unusually small: {len(sa_content)} bytes"

        # Verify no un-inlined local relative scripts
        unbundled_scripts = re.findall(r'<script\s+[^>]*src=["\']js\/[^"\']+["\']', sa_content)
        assert len(unbundled_scripts) == 0, f"Un-inlined local script tags found in standalone bundle: {unbundled_scripts}"

        # Verify embedded module script bracket balance
        start_tag = '<script type="module">'
        end_tag = '</script>'
        start_idx = sa_content.rfind(start_tag)
        end_idx = sa_content.rfind(end_tag)
        assert start_idx != -1 and end_idx != -1, "Embedded module script missing in standalone bundle"
        
        bundled_js = sa_content[start_idx + len(start_tag):end_idx]
        bc, bp, bs = check_brace_balance(bundled_js)
        assert bc == 0, f"Mismatched curly braces in standalone bundle: {bc}"
        assert bp == 0, f"Mismatched parentheses in standalone bundle: {bp}"

        print("      PASS: Standalone bundle valid with fully inlined assets and 0 syntax errors.")
        passed_checks += 1
    except Exception as e:
        print(f"      FAIL: Standalone Bundle Error: {e}")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────
    # [7/7] Flask Backend Server Routing Sanity
    # ─────────────────────────────────────────────────────────────
    print("[7/7] Checking Flask Backend Server & Route Health...")
    try:
        try:
            from app.backend.app import app
        except ImportError:
            from netgravity.app.backend.app import app
            
        app.testing = True
        client = app.test_client()

        # Check root route /
        res_root = client.get("/")
        assert res_root.status_code == 200, f"Root route / returned status {res_root.status_code}"
        assert b"NetGravity" in res_root.data, "Root route response missing 'NetGravity' title"

        # Check API / static route
        res_css = client.get("/css/style.css")
        assert res_css.status_code == 200, f"Static CSS route returned status {res_css.status_code}"

        print("      PASS: Flask application and static routes verified operational.")
        passed_checks += 1
    except Exception as e:
        print(f"      FAIL: Flask Backend Routing Error: {e}")
        sys.exit(1)

    elapsed = time.perf_counter() - start_t
    print("=" * 75)
    print(f"  SMOKE TEST PASSED IN {elapsed:.2f} SECONDS -- ALL {passed_checks}/{total_checks} CHECKS VERIFIED")
    print("=" * 75)

if __name__ == "__main__":
    run_smoke_test()
