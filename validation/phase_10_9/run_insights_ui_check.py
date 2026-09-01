"""
Browser check: are insights actually visible for an uploaded dataset?

The reported symptom. Signs in, opens the demo project, waits for the analysis
to finish, and reads the Home attention feed and the recommendation block off
the live page. Then opens a card and reads the deep dive.

Fails loudly on a console error or a CSP violation, because the whole point of
this path is that it now runs in the browser rather than existing in the
backend.
"""
from __future__ import annotations

import json
import sys
import time

BASE = "http://127.0.0.1:5050"
EMAIL = "insight.check@example.com"
PASSWORD = "netgravity-check-123"

results = []


def check(cid, description, ok, detail=""):
    results.append((cid, description, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {cid:6} {description}"
          + (f" - {detail}" if detail else ""), flush=True)


def main() -> int:
    from playwright.sync_api import sync_playwright

    violations, page_errors, console_errors = [], [], []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)
        page.on("response", lambda r: violations.append(r.url)
                if r.status >= 500 else None)

        page.goto(BASE, wait_until="networkidle")

        # ---- sign in (or sign up) ----------------------------------------
        page.evaluate("""async ([email, password]) => {
            const csrf = () => (document.cookie.match(/ng_csrf=([^;]*)/) || [])[1] || '';
            const post = (url, body) => fetch(url, {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf()},
                body: JSON.stringify(body),
            });
            let r = await post('/api/auth/login', {email, password});
            if (!r.ok) {
                await post('/api/auth/signup',
                           {email, password, name: 'Insight Check'});
                r = await post('/api/auth/login', {email, password});
            }
            return r.status;
        }""", [EMAIL, PASSWORD])
        page.reload(wait_until="networkidle")

        # ---- open a project ---------------------------------------------
        projects = page.evaluate("""async () => {
            const r = await fetch('/api/projects', {credentials: 'include'});
            const d = await r.json();
            return (d.projects || []).map(p => ({id: p.id, name: p.name}));
        }""")
        if not projects:
            check("I-00", "a project exists to analyse", False, "none returned")
            return 1
        project = projects[0]
        check("I-00", "a project exists to analyse", True, project["name"])

        # Open it the way the application does: mark it active, then reload so
        # `restoreSession()` -> `loadProjects()` -> `openProjectById()` runs.
        # Calling `openProjectById` directly returns false when the app's own
        # project list has not been loaded, which is how this harness previously
        # measured an empty dashboard that no user would ever see.
        page.evaluate("""async (id) => {
            const ctx = await import('/js/integration/project-context.js');
            ctx.setActiveProject(id);
        }""", project["id"])
        page.reload(wait_until="networkidle")

        # ---- wait for the analysis, including the insights stage --------
        deadline = time.time() + 240
        insight_count = 0
        while time.time() < deadline:
            state = page.evaluate("""() => {
                const list = document.getElementById('home-attention-list');
                const rec = document.getElementById('home-recommendation');
                return {
                    overlay: !!document.querySelector('.ing-loading-overlay.active,'
                             + ' #loading-modal-overlay.active'),
                    cards: list ? list.querySelectorAll('.home2-attn-item').length : -1,
                    emptyText: list ? (list.innerText || '').slice(0, 120) : '',
                    recText: rec ? (rec.innerText || '').slice(0, 400) : '',
                };
            }""")
            insight_count = state["cards"]
            if insight_count > 0:
                break
            time.sleep(2)

        state = page.evaluate("""() => {
            const list = document.getElementById('home-attention-list');
            const rec = document.getElementById('home-recommendation');
            const cards = [...(list ? list.querySelectorAll('.home2-attn-item') : [])];
            return {
                count: cards.length,
                titles: cards.map(c => (c.querySelector('.home2-attn-item-title')
                                        || {}).innerText || ''),
                kickers: cards.map(c => (c.querySelector('.home2-attn-kicker')
                                         || {}).innerText || ''),
                subs: cards.map(c => (c.querySelector('.home2-attn-item-sub')
                                      || {}).innerText || ''),
                emptyText: list ? (list.innerText || '').slice(0, 160) : '',
                recText: rec ? (rec.innerText || '') : '',
                riskLines: document.querySelectorAll('.home2-attn-risk-line').length,
                riskLineTexts: [...document.querySelectorAll('.home2-attn-risk-line')]
                    .map(e => (e.innerText || '').trim()),
            };
        }""")

        check("I-01", "the attention feed renders at least one insight",
              state["count"] > 0,
              f"{state['count']} cards; empty text = {state['emptyText']!r}")
        check("I-02", "insights carry real titles",
              all(t.strip() for t in state["titles"]),
              json.dumps(state["titles"][:6]))
        check("I-03", "each card carries a category from the engine's severity",
              all(k.strip() for k in state["kickers"]),
              json.dumps(state["kickers"][:6]))
        check("I-04", "each card carries a subtitle drawn from the narrative",
              all(s.strip() for s in state["subs"]),
              json.dumps([s[:60] for s in state["subs"][:3]]))
        titles_lower = [t.strip().lower() for t in state["titles"]]
        check("I-05b", "no finding is shown twice",
              len(titles_lower) == len(set(titles_lower)),
              json.dumps([t for t in set(titles_lower)
                          if titles_lower.count(t) > 1]))
        check("I-06", "a recommendation is shown",
              "recommend" in state["recText"].lower(),
              state["recText"][:180].replace("\n", " | "))

        # ---- the API behind it ------------------------------------------
        api = page.evaluate("""async (pid) => {
            const r = await fetch(`/api/insights?project_id=${pid}&scope=NETWORK`,
                                  {credentials: 'include'});
            return {status: r.status, body: await r.json()};
        }""", project["id"])
        body = api["body"] or {}

        # The prototype filled this slot with `8 + hash(id) % 24` lakh — a
        # rupee figure in the most prominent position on the card, invented
        # from the insight's id. This used to assert the line was absent, which
        # forbade the fabrication by forbidding the element.
        #
        # The slot now carries the finding's own first evidence value, exactly
        # as the engine formatted it. So the check is what it always meant:
        # every rendered line must be a figure the API actually returned. A
        # hashed value fails this, and so would any figure the frontend derived
        # for itself.
        # Every scope the feed draws from, not just the network one: Home
        # merges the network briefing with the selected facility's, so a card
        # may carry a figure that only the FACILITY response contains.
        scoped = page.evaluate('''async (pid) => {
            const facility = (document.getElementById('home-top-facility') || {}).value || '';
            const out = [];
            const net = await fetch(`/api/insights?project_id=${pid}&scope=NETWORK`,
                                    {credentials: 'include'});
            out.push(await net.json());
            if (facility && facility !== 'ALL') {
                const f = await fetch(
                    `/api/insights?project_id=${pid}&scope=FACILITY&entity_id=${encodeURIComponent(facility)}`,
                    {credentials: 'include'});
                if (f.ok) out.push(await f.json());
            }
            return out;
        }''', project["id"])

        api_values = set()
        for _payload in (scoped or []):
            for _ins in ((_payload or {}).get("insights") or []):
                for _e in (_ins.get("evidence") or []):
                    if _e.get("display_value"):
                        api_values.add(str(_e["display_value"]).strip())
        unbacked = [t for t in state.get("riskLineTexts", []) if t not in api_values]
        check("I-05", "every headline figure on a card comes from the engine",
              not unbacked,
              f"{state['riskLines']} lines rendered; "
              f"unbacked={json.dumps(unbacked[:4])}")
        check("I-07", "/api/insights answers 200", api["status"] == 200,
              f"status={api['status']}")
        check("I-08", "the response carries themed insights",
              len(body.get("insights") or []) >= 3,
              json.dumps([i.get("theme") for i in body.get("insights") or []]))
        check("I-09", "every insight states a severity",
              all(i.get("severity") in {"RISK", "OPPORTUNITY", "INFORMATION"}
                  for i in body.get("insights") or []),
              json.dumps([i.get("severity") for i in body.get("insights") or []]))
        check("I-10", "the narrative's figures passed numeric grounding",
              (body.get("grounding") or {}).get("status") in
              {"GROUNDED", "NO_CLAIMS"},
              json.dumps(body.get("grounding")))
        check("I-11", "insights cite evidence with authoritative values",
              any(i.get("evidence") for i in body.get("insights") or []),
              json.dumps([[e.get("label") for e in (i.get("evidence") or [])]
                          for i in (body.get("insights") or [])[:3]]))
        check("I-12", "no LLM was used to produce them",
              (body.get("provenance") or {}).get("llm_used") is False,
              json.dumps(body.get("provenance")))

        # ---- the deep dive ----------------------------------------------
        if state["count"] > 0:
            page.evaluate("""() => {
                const first = document.querySelector('#home-attention-list .home2-attn-item');
                if (first) first.click();
            }""")
            page.wait_for_timeout(1200)
            deep = page.evaluate("""() => {
                const page_ = document.getElementById('tab-insight-detail');
                if (!page_) return null;
                return {
                    visible: page_.classList.contains('active'),
                    title: (page_.querySelector('.insd-title') || {}).innerText || '',
                    badge: (page_.querySelector('.insd-badge') || {}).innerText || '',
                    // The narrative moved into the chart card's note when the
                    // deep dive gained a chart. `.insd-why-text` now holds the
                    // recommendation's caveat, so reading it here made this
                    // check pass while no longer verifying its own claim.
                    found: (page_.querySelector('.insd-chart-note') || {}).innerText || '',
                    whyText: (page_.querySelector('.insd-why-text') || {}).innerText || '',
                    chartCanvas: !!page_.querySelector('#insd-trend-chart'),
                    evidenceRows: page_.querySelectorAll('.insd-table tbody tr').length,
                    rec: (page_.querySelector('.insd-rec-sentence') || {}).innerText || '',
                    text: page_.innerText || '',
                };
            }""")
            check("I-13", "the deep dive opens on the finding", bool(deep and deep["visible"]),
                  (deep or {}).get("title", "")[:80])
            check("I-14", "it states what was found",
                  bool(deep and deep["found"].strip()),
                  (deep or {}).get("found", "")[:120])
            check("I-15", "it shows evidence rows or says it cites none",
                  bool(deep) and (deep["evidenceRows"] > 0
                                  or "cites no single figure" in deep["text"]),
                  f"{(deep or {}).get('evidenceRows')} rows")
            # The fabrications that used to fill this page.
            banned = ["Priya Mehta", "Regional Planning Analyst", "at risk",
                      "Generate Analyst Email", "94.6", "Approve recommendation"]
            present = [b for b in banned if deep and b in deep["text"]]
            check("I-16", "no fabricated figures, charts or e-mail remain",
                  not present, json.dumps(present))

        check("I-17", "no page errors", not page_errors,
              json.dumps(page_errors[:3]))
        csp = [c for c in console_errors if "Content Security Policy" in c]
        check("I-18", "no CSP violations", not csp, json.dumps(csp[:3]))
        check("I-19", "no 5xx responses", not violations,
              json.dumps(violations[:3]))

        page.screenshot(path="insights_home.png", full_page=False)
        browser.close()

    passed = sum(1 for r in results if r[2])
    print(f"\n{passed} passed, {len(results) - passed} failed of {len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
