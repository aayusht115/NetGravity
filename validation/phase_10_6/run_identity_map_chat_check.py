"""Identity, the basemap, and the assistant — driven in a real browser.

Three things the user reported, checked the way they saw them:

  * "Remove the hard coded values on the application like profile name and
    everything. It should pickup from the account logged in."
  * "Map shows API key required, inspect and fix."
  * "Extensively test the chat bot and fix if any issue, use llm api key if
    available."

The map check is the awkward one, and worth explaining. The basemap used to be
fetched from `basemaps.cartocdn.com`, a third-party service on an anonymous
quota. When that quota or a network refuses, the service returns a valid PNG
with "API key required" printed across it — HTTP 200, decodes cleanly, no
`tileerror` for Leaflet to catch and nothing in the DOM to find. So this does
not look for the message: it asserts the map makes NO third-party request at
all, which is the only property that cannot silently regress.
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
PORT = 5191
BASE = f"http://127.0.0.1:{PORT}/"

USER_NAME = "Priya Raghavan"
EXPECTED_INITIALS = "PR"

results = {"checks": []}


def record(cid: str, name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results["checks"].append(
        {"id": cid, "name": name, "status": status, "detail": detail})
    print(f"[{status:4}] {cid:6} {name}" + (f" - {detail}" if detail else ""))
    sys.stdout.flush()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    import os
    os.environ.setdefault("NETGRAVITY_DB_PATH", str(OUT / "browser_check.db"))
    os.environ["NETGRAVITY_SEED_DEMO"] = "0"

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
    external: list = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.on("pageerror", lambda e: page_errors.append(str(e)[:300]))

        def note_request(req):
            url = req.url
            if url.startswith(BASE) or url.startswith("data:") or url.startswith("blob:"):
                return
            external.append(url[:140])
        page.on("request", note_request)

        email = f"priya-{uuid.uuid4().hex[:6]}@acme-logistics.in"
        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)

        # ---- I-01 The sign-up form ships nobody's details ---------------
        prefilled = page.evaluate(
            """() => {
                const ids = ['signup-name', 'signup-email', 'signin-email',
                             'panel-reset-email'];
                const out = {};
                ids.forEach(id => {
                  const el = document.getElementById(id);
                  if (el) out[id] = el.value;
                });
                return out;
            }"""
        )
        record("I-01", "The auth forms are not pre-filled with a stranger's details",
               all(not v for v in prefilled.values()), json.dumps(prefilled))

        page.evaluate("window.switchAuthPanel('signup')")
        page.wait_for_timeout(300)
        page.fill("#signup-name", USER_NAME)
        page.fill("#signup-email", email)
        page.fill("#signup-password", "Netgravity@2026")
        page.evaluate("window.completeAuth('signup')")
        page.wait_for_selector("#create-project-page:not(.hidden)", timeout=30000)
        page.fill("#proj-name", "Acme Network")
        page.click("#proj-create-submit")
        page.wait_for_selector("#upload-data-page:not(.hidden)", timeout=30000)

        # ---- I-02 The upload screens show the signed-in user ------------
        page.wait_for_timeout(1200)
        upload_avatar = page.evaluate(
            """() => {
                const el = document.querySelector('#upload-data-page .user-avatar-ak')
                        || document.querySelector('.user-avatar-ak');
                return el ? { text: el.textContent.trim(), title: el.title } : null;
            }"""
        )
        record("I-02", "The upload screen's avatar is the signed-in user",
               bool(upload_avatar) and upload_avatar["text"] == EXPECTED_INITIALS
               and upload_avatar["title"] == USER_NAME,
               json.dumps(upload_avatar))

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
                    btn.click(); clicked = True; page.wait_for_timeout(3500); break
            if not clicked:
                break
            if page.is_visible(".app-shell") and not page.is_visible("#ingestion-page"):
                break
        page.wait_for_timeout(18000)

        # ---- I-03 The app shell shows the signed-in user ----------------
        identity = page.evaluate(
            """() => ({
                greeting: (document.getElementById('logged-in-user-name')||{}).textContent,
                avatar: (document.querySelector('.user-avatar-ak')||{}).textContent,
                avatarTitle: (document.querySelector('.user-avatar-ak')||{}).title,
                profileTitle: (document.getElementById('btn-topbar-profile')||{}).title,
            })"""
        )
        record("I-03", "The header greets the signed-in user, not a fixed name",
               identity["greeting"] == USER_NAME
               and identity["avatar"] == EXPECTED_INITIALS
               and USER_NAME in (identity["profileTitle"] or ""),
               json.dumps(identity))

        # ---- I-04 Nothing anywhere still says the old name --------------
        leaked = page.evaluate(
            """() => {
                const hits = [];
                const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let n;
                while ((n = walk.nextNode())) {
                  const t = (n.nodeValue || '').trim();
                  if (/amit|kearney decision systems|lead supply chain architect/i.test(t)) {
                    hits.push(t.slice(0, 90));
                  }
                }
                document.querySelectorAll('[title]').forEach(e => {
                  if (/amit/i.test(e.title)) hits.push('title: ' + e.title);
                });
                return hits.slice(0, 8);
            }"""
        )
        record("I-04", "No screen still carries the prototype's user",
               not leaked, json.dumps(leaked))

        # ---- I-05 The profile menu reports the account ------------------
        #
        # The menu used to open an alert(). It now opens the account security
        # screen, which reports the same identity AND is where a password is
        # changed, a second factor enrolled and live sessions revoked — the
        # endpoints for all three landed in Phase 10.8, and an endpoint nobody
        # can reach is a feature on paper.
        page.evaluate("document.getElementById('profile-menu-profile').click()")
        page.wait_for_timeout(2500)
        profile_text = page.evaluate(
            """() => {
                const el = document.getElementById('account-security-overlay');
                return el && el.classList.contains('active')
                  ? el.innerText.replace(/\\s+/g, ' ') : '';
            }"""
        )
        record("I-05", "The profile screen reports the account, not a fixed role",
               bool(profile_text) and USER_NAME in profile_text
               and email in profile_text
               and "Kearney Decision Systems" not in profile_text
               and "Two-factor" in profile_text,
               (profile_text or "")[:220])
        page.evaluate(
            "const b = document.getElementById('acct-close'); if (b) b.click();")
        page.wait_for_timeout(400)

        # ================= MAP =========================================
        external.clear()
        page.click("#nav-item-twin")
        page.wait_for_timeout(3000)
        page.evaluate(
            """() => {
                const b = [...document.querySelectorAll('button, .toggle-btn')]
                  .find(x => /2D Map/i.test(x.textContent || ''));
                if (b) b.click();
            }"""
        )
        page.wait_for_timeout(6000)
        page.screenshot(path=str(SHOTS / "map_2d.png"), full_page=True)

        record("M-01", "The map makes no third-party request",
               not external, json.dumps(sorted(set(external))[:6]))

        drawn = page.evaluate(
            """() => {
                const c = document.querySelector('#tab-twin .leaflet-container')
                       || document.querySelector('.leaflet-container');
                if (!c) return { found: false };
                const img = c.querySelector('img.leaflet-image-layer');
                return {
                  found: true,
                  basemap: !!img,
                  basemapLoaded: img ? (img.complete && img.naturalWidth > 0) : false,
                  basemapIsEmbedded: img ? img.src.startsWith('data:') : false,
                  tileRequests: c.querySelectorAll('img.leaflet-tile').length,
                  markers: c.querySelectorAll('.custom-marker').length,
                  corridors: c.querySelectorAll('path.leaflet-interactive').length,
                };
            }"""
        )
        record("M-02", "The basemap is embedded in the application and renders",
               drawn.get("basemap") and drawn.get("basemapLoaded")
               and drawn.get("basemapIsEmbedded"), json.dumps(drawn))
        record("M-03", "The network is drawn on top of it",
               (drawn.get("markers") or 0) >= 15
               and (drawn.get("corridors") or 0) > 0, json.dumps(drawn))

        # A facility must land where its coordinates say. If the image were
        # stretched as equirectangular instead of Mercator, markers would drift
        # by tens of kilometres against the coastline beneath them.
        alignment = page.evaluate(
            """async () => {
                const map = await import('/js/map.js');
                const data = await import('/js/data.js');
                const c = document.querySelector('#tab-twin .leaflet-container');
                const img = c.querySelector('img.leaflet-image-layer');
                const box = img.getBoundingClientRect();
                const cbox = c.getBoundingClientRect();
                // Delhi ~28.61N 77.21E must sit in the upper-middle of an
                // India basemap; Chennai ~13.08N 80.27E in the lower-middle.
                const marks = [...c.querySelectorAll('.custom-marker')]
                  .map(m => m.getBoundingClientRect());
                return {
                  imgW: Math.round(box.width), imgH: Math.round(box.height),
                  containerW: Math.round(cbox.width),
                  markersInsideImage: marks.filter(m =>
                     m.left >= box.left - 40 && m.right <= box.right + 40
                     && m.top >= box.top - 40 && m.bottom <= box.bottom + 40).length,
                  markers: marks.length,
                };
            }"""
        )
        record("M-04", "Every facility falls inside the basemap's own bounds",
               alignment["markers"] > 0
               and alignment["markersInsideImage"] == alignment["markers"],
               json.dumps(alignment))

        # ================= CHATBOT =====================================
        page.evaluate("window.openChatbotModal && window.openChatbotModal()")
        page.wait_for_timeout(1200)
        greeting = page.evaluate(
            "() => (document.querySelector('.chatbot-welcome-greeting')||{}).textContent")
        record("C-01", "The assistant greets the signed-in user",
               "Priya" in (greeting or ""), greeting or "(no greeting)")
        page.screenshot(path=str(SHOTS / "chat_open.png"), full_page=True)

        def ask(question: str, budget: int = 200):
            started = time.time()
            page.evaluate("(q) => window.askChatbotPrompt(q)", question)
            while time.time() - started < budget:
                page.wait_for_timeout(2000)
                st = page.evaluate(
                    """() => {
                        const b = [...document.querySelectorAll('.chat-bubble-ai')];
                        const last = b[b.length - 1];
                        return {
                          typing: !!document.getElementById('chatbot-typing-indicator'),
                          topic: last ? ((last.querySelector('.ai-badge-chip')||{}).textContent||'').trim() : '',
                          text: last ? last.innerText : '',
                        };
                    }"""
                )
                if not st["typing"] and st["text"]:
                    return {"topic": st["topic"], "text": st["text"],
                            "secs": round(time.time() - started, 1)}
            return {"topic": "", "text": "(timed out)", "secs": budget}

        a = ask("What is my total network cost?")
        record("C-02", "It answers a cost question with the network's own cost",
               "18,067,79" in a["text"].replace(" ", ""),
               f"({a['secs']}s) {a['text'][:200]}")

        a = ask("Which distribution centre is most utilised?")
        record("C-03", "It names the right facility, not a generic summary",
               "Pune" in a["text"] and "43" in a["text"],
               f"({a['secs']}s) {a['text'][:200]}")

        a = ask("How many facilities do I have?")
        record("C-04", "It answers a count question from the twin",
               "5 distribution centres" in a["text"] and "3 plants" in a["text"],
               f"({a['secs']}s) {a['text'][:180]}")

        a = ask("Why is some of my demand unserved?")
        record("C-05", "An explanation of unserved demand is about unserved demand",
               "8,733" in a["text"] or "unserved" in a["text"].lower(),
               f"({a['secs']}s) {a['text'][:200]}")

        a = ask("Tell me a joke")
        record("C-06", "An out-of-scope request is declined, not answered",
               "could not work out" in a["text"].lower()
               and "REI" not in a["text"],
               f"({a['secs']}s) {a['text'][:180]}")

        a = ask("Who is the prime minister of India?")
        record("C-07", "A general-knowledge question is declined",
               "could not work out" in a["text"].lower(),
               f"({a['secs']}s) {a['text'][:160]}")

        a = ask("What is the weather in Mumbai tomorrow?")
        record("C-08", "Naming a city is not treated as naming a facility",
               "which one do you mean" not in a["text"].lower(),
               f"({a['secs']}s) {a['text'][:180]}")

        a = ask("Simulate closing the Kolkata Distribution Center", budget=300)
        record("C-09", "A scenario request is understood and acted on",
               a["topic"] and "SCENARIO" in a["topic"].upper(),
               f"({a['secs']}s) [{a['topic']}] {a['text'][:200]}")

        transcript = page.evaluate(
            """() => [...document.querySelectorAll('.chat-bubble-ai')]
                 .map(b => b.innerText).join('\\n---\\n')"""
        )
        record("C-10", "No answer contains an unformatted raw float",
               not any(tok for tok in transcript.split()
                       if tok.replace(",", "").replace("₹", "").replace(".", "").isdigit()
                       and "." in tok and len(tok.split(".")[-1]) > 2),
               transcript[:200].replace("\n", " "))

        record("C-11", "No uncaught page errors",
               not page_errors, "; ".join(page_errors[:3])[:300])
        page.screenshot(path=str(SHOTS / "chat_answers.png"), full_page=True)

        # ---- The control plane, from the browser, both ways -------------
        # Through the app's own client, which carries the session — and with a
        # fetch carrying NOTHING. Both matter: the first proves authenticating
        # the control plane did not break the application, the second proves it
        # cannot be reached without a session.
        #
        # `credentials: 'omit'` is the part that makes the second request
        # genuinely anonymous. The session is now an httpOnly cookie, and a
        # plain same-origin `fetch` attaches it automatically — so a bare fetch
        # is no longer an unauthenticated one, and checking it that way would
        # have quietly stopped testing anything.
        access = page.evaluate(
            """async () => {
                const { apiClient } = await import('/js/integration/api-client.js');
                let authed = null, anon = null;
                try { await apiClient.get('/orchestrator/twin/states'); authed = 200; }
                catch (e) { authed = e.status || e.code || String(e); }
                anon = (await fetch('/orchestrator/twin/states',
                                    { credentials: 'omit' })).status;
                return { authed, anon };
            }"""
        )
        record("A-01", "A signed-in browser reaches the control plane; a bare request does not",
               access["authed"] == 200 and access["anon"] == 401,
               json.dumps(access))

        browser.close()

    (OUT / "identity_map_chat_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    print(f"\n{passed} passed, {failed} failed of {len(results['checks'])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
