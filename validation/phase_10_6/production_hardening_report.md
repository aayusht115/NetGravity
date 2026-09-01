# Phase 10.6 — Authentication, durability, identity, the basemap, the assistant

Five items. Two of them were the reasons previous phases refused to call this
application production-ready.

---

## 1. The control plane was open to anyone

`/orchestrator/*` was mounted with no authentication of any kind:

```python
app.register_blueprint(create_orchestrator_blueprint(_orchestrator))
```

Anyone who could reach the port could run a solve, read any Digital Twin state
in the process, read the full decision trace of any execution — and, through
`POST /orchestrator/approvals/<id>`, **approve a governed action**, because the
actor was taken from the request body:

```python
payload["actor"] = Actor(**actor_raw)     # role: "APPROVER", if you like
```

`resolve_approval` checks `actor.role` to decide whether a HUMAN_ONLY structural
change may proceed. A caller could grant themselves the role in the same request
that used it.

### What changed

* `create_orchestrator_blueprint` takes an `authenticator` and **fails closed**
  without one: every route returns 401. The default is now the safe one.
* The guard is a **`before_request` on the blueprint**, not a decorator per
  route. The failure being fixed is a route nobody decorated, so there must be
  no way to add an endpoint and forget it.
* The actor is built from the session by `bearer_actor()`, and any `actor` in
  the body is discarded. An account role the store does not recognise maps to
  `VIEWER` — the least authority, not the most.
* `netgravity/` still imports nothing from the application: the authenticator is
  passed in, exactly as the persistence hooks are.

Two tests mounted the blueprint directly and one asserted a 200 for an anonymous
call. They now supply an explicit test identity, and a new test walks **ten**
control-plane routes asserting 401 for a caller with no session.

---

## 2. Nothing survived a restart

Every store in the application layer was a dictionary in the process. Restarting
the server discarded every account, session, project, uploaded network and
solved scenario. A user came back to a sign-up form.

`app/backend/services/persistence.py` is a SQLite store — one file, one
connection per thread, WAL journaling so a read never blocks the write a
40-second solve is holding open. `durability.py` is the single seam where each
store is bound to it and reloaded, and it is the only file that knows both
sides.

| Restored on start-up | Where it lives |
|---|---|
| Accounts and live sessions | `AuthService` |
| Projects and their snapshot binding | `ProjectRegistry` |
| Uploaded network snapshots | `SnapshotManager` |
| Materialised scenario networks | `ScenarioStore` |
| Solved scenario records | the scenarios blueprint |
| Demand, capacity and signal history | the upload stores |

The engine package keeps no dependency on the application: `SnapshotManager` and
`ScenarioStore` expose a `persist_hook`/`restore_hook` pair and know nothing
about SQLite. Unset, they behave exactly as before.

**Deliberately not persisted:** execution contexts and audit traces. An
execution is the record of one in-flight run holding typed solver results and
step state; its *artefacts* are stored individually, so a restart loses the
workings and keeps the answers. Reloading a process's stack into a different
process is not meaningful, and pretending otherwise would be worse than saying
so.

### Two bugs this found

**The stored document had the wrong shape.** `ProjectRecord.to_dict()` is the
API projection — it renames `project_id` to `id` for the client. Persisting it
looked correct and restored **nothing**, because the loader looks for
`project_id` and every row had `id`. There is now a separate `stored()`, as
`User` already had, so the two shapes cannot stand in for one another.

**The test suite would have started failing on its second run.** Accounts now
persist, and `test_signup_success` registers a fixed address that signup
correctly refuses the second time. The root `conftest.py` now points
`NETGRAVITY_DB_PATH` at a per-process temp file *at import time* — a fixture is
too late, because the connection opens when `app.backend.app` is imported during
collection. A test that passes only on a clean machine is not a passing test.

### Proof

`validation/phase_10_6/run_persistence_check.py` builds real state through the
HTTP API, drops **every** `app.backend` and `netgravity.orchestrator` module
from `sys.modules`, rebuilds the application from scratch against the same file,
and asks for the same things back. Reusing the imported module would prove
nothing — the dictionaries would still be full.

**14 / 14.** A session issued before the restart still authenticates; the
password hash still rejects a wrong password; the uploaded network comes back
byte-for-byte (`data_version f80ffaf7a09273e2`) and re-solves to the same
₹18,067,793.96; a solved scenario keeps its figures *and* can still say what it
changed — which only passes because the materialised scenario network was
persisted too; a revoked session stays revoked; and ownership is still enforced
on restored projects.

---

## 3. The application was one person

The signed-in user's name appeared nowhere. A fixed identity was spelled out in
six places:

| Where | What it said |
|---|---|
| `index.html` header | `Hello, <strong>Amit Kumar</strong>` |
| `index.html` avatar | `<div class="user-avatar-ak">AK</div>` |
| `index.html` profile button | `title="User Profile: Amit Kumar"` |
| `index.html` assistant | `<h4>Hi Amit! 👋</h4>` |
| `app.js` profile menu | `Role: Lead Supply Chain Architect (Admin)` / `Organization: Kearney Decision Systems` |
| `ingestion.js` upload header | `<div class="user-avatar-ak" title="Amit Kumar">AK</div>` |

None of it moved when somebody else signed in. The profile menu was the worst of
them: it stated a **role**, which is a security-relevant claim the server had
never made about that account.

`js/identity.js` is now the single owner. `/api/auth/me` is the authority — the
client never assembles an identity from what was typed into the signup form, so
a name the server normalised is the one shown. It writes the greeting, the
avatar initials, the profile tooltip and the assistant's greeting, and re-applies
on an `identityChanged` event for the screens that render later (the upload
header re-rendered several times per upload, restoring "AK" each time).

The four auth forms also shipped pre-filled with `amit.kumar@kearney.com` as a
`value`, not a placeholder — a stranger's address typed into every new user's
sign-up form.

**Sign-out now signs you out.** It called `returnToLanding()` and nothing else:
the bearer token stayed in `localStorage` and the previous user's network stayed
in memory. With session restore added this phase, a refresh would have put the
next person straight back into that account. It now revokes the session
server-side, clears the token, the identity, the network model and the active
project.

---

## 4. "Map shows API key required"

The 2D map fetched its basemap from `https://{s}.basemaps.cartocdn.com/light_all/…`
— a third-party service on an anonymous quota.

The failure mode is the interesting part. When that quota or a corporate network
refuses, the service does **not** return an error. It returns a valid PNG with
"API key required" printed across it: HTTP 200, decodes cleanly, no `tileerror`
for Leaflet to catch, and nothing in the DOM to find. The client's own
facilities were plotted on top of a watermark announcing that their software was
misconfigured, and no amount of error handling inside the application could have
detected it. It renders correctly on this machine, which is exactly why it
cannot be fixed by testing the message.

The map now draws on `INDIA_BASEMAP_DATA_URI` — an India basemap embedded in the
application, and the same image the 3D twin already stands on, so the two views
agree. No key, no quota, no internet, nothing that can change underneath it.

**Why an image overlay is exact here and not an approximation:**
`L.ImageOverlay` stretches the image linearly between its corners in the map's
own *projected* space, which for Leaflet's default CRS is Web Mercator. The
embedded image is itself a Web Mercator crop taken between exactly those corners
— `twin3d.js` reprojects with the same constants. A linear stretch between the
projected corners of a Mercator crop reproduces the crop, so every facility
lands on the pixel its coordinates belong to. An equirectangular image would
visibly bow the coastline across India's 35° of latitude.

Live tiles remain one setting away: `CONFIG.MAP_TILE_URL` takes any tile
template — your own server, or a keyed provider with the key already in the URL
— and lifts the zoom cap with it.

The check asserts the property that cannot silently regress: **the map makes no
third-party request at all.** Measured: 0 external requests, 15 markers, 36
corridors, all 15 facilities inside the basemap's own bounds.

---

## 5. The assistant

### It never received the question

This is the one that made the assistant feel broken. The reasoning prompt said:

> *"Explain what they mean for the business"*

and nothing else. **The user's question was never in it.** So every question got
the same executive briefing:

| Asked | Answered |
|---|---|
| Which distribution centre is most utilised? | *"I see a business network cost of 18,067,793.96 per period… I see 8,733 units of demand that cannot be served."* |
| Why is some of my demand unserved? | *"I see the highest relative economic exposure at F003 (REI 1.00)…"* |

Every figure was correct. Neither addressed the question, which is the most
misleading shape a wrong answer can take.

The question now reaches the prompt, placed after the evidence and immediately
before the response contract, bounded so a long paste cannot displace the
instructions, with an explicit instruction to say plainly when the results do
not contain what was asked rather than answering something else.

### It could not see a single facility

`flatten_network_state()` drops per-facility detail by design — the flattened
dict is a transport projection of totals and id lists. But that dict *was* the
whole reasoning payload, so "which DC is most utilised?" reached a narrator
holding the network's average utilisation and not one facility's. It could not
have answered.

The payload now carries the solver's own per-facility rows.

> **Before:** *"I see a business network cost of 18,067,793.96 per period…"*
> **After:** *"I find Pune Distribution Center is the most utilised distribution
> centre at 43.1%. Its throughput is 9,762 units."*

**One thing was deliberately left out: stated capacity.** Everything in the
payload becomes an authoritative fact for numeric grounding, and grounding
matches on *kind* — currency, units, count — not on metric name, because
narratives move between them. Every number added widens the space a claim can
match. Capacity is an input the user uploaded, not a result of the solve, and
including it was enough for a fixture plant with a stated capacity of 99,999 to
make a hallucinated cost of "99,999.00" verify as **grounded**. The regression
suite caught it. Outputs earn a place in the fact space; inputs do not.

### It could not decline

`Intent.UNKNOWN` was filtered out of the classifier's list of valid intents, so
the model had no way to say "this is not a supply-chain question" and picked the
nearest thing. "Tell me a joke" and "Who is the prime minister of India?" both
came back `EXPLANATION` with high confidence, and were answered with a briefing
about facility F003's economic exposure.

UNKNOWN is now offered, with a rule saying it is a *correct* answer and
preferred to a confident guess. The refusal path already existed and was well
written; nothing could reach it.

### Three more, each with its own cause

**Any short sentence became a follow-up.** `_is_elliptical` returned True for
anything under seven words, so "Tell me a joke" — classified UNKNOWN — was
promoted to EXPLANATION as a question about the previous answer. A genuine
follow-up *refers back*: it continues the previous sentence, points at it, or is
a fragment with no verb of its own. A short message with its own subject and
verb and no back-reference is a new request, and is left UNKNOWN.

**Naming a city was treated as naming a facility.** "What is the weather in
Mumbai tomorrow?" was met with *"I found 2 facilities matching 'mumbai': F001,
M002. Which one do you mean?"* — a clarification implying the question was
understood and only the subject was unclear. An UNKNOWN request has no action to
disambiguate *for*, so it now joins the intents that skip entity ambiguity.

**"Why is my demand unserved?" was answered from the wrong evidence.**
`wf_explanation` is deliberately REI-only and runs no optimization —
`test_a_cost_explanation_does_not_launch_an_optimization` pins that, and it is
right: an explanation must not silently start work the user did not ask for, and
a cost question *can* be answered from exposure evidence. Unserved demand
cannot: no amount of REI data contains the reason some demand could not be
served. The routing change is deliberately narrow — feasibility vocabulary only,
risk vocabulary still wins, cost and utilisation explanations unchanged.

### And a boot error, found on the way

```
initHomeSelectors error: TypeError: Cannot read properties of undefined (reading 'id')
    at populateFacilitySelector (app.js:752)
```

`facilities[0].id` on an empty network — the ordinary state at boot. It aborted
`initHomeSelectors()`, leaving every Home selector unwired until a network
happened to load first.

---

## 6. Validation

| Suite | Result |
|---|---|
| `validation/phase_10_6/run_persistence_check.py` (new) | **14 / 14** |
| `validation/phase_10_6/run_identity_map_chat_check.py` (new, browser) | **21 / 21** |
| `validation/phase_10_5/run_scenario_ui_check.py` | **29 / 29** |
| `validation/phase_10_5/run_scenario_types_check.py` | **15 / 15** |
| `validation/phase_10_4/run_scenario_check.py` | **11 / 11** |
| `validation/phase_10_3/run_ui_flow_check.py` | **25 / 25** |
| `validation/phase_10_3/run_empty_project_check.py` | **10 / 10** |
| `validation/phase_10_1/run_client_data_e2e.py` | **27 / 27** |
| Backend regression | **2,544 passed · 4 skipped** in 140s |

Assistant timings on the client network, through the browser: a cost question
53s, a facility question 39s (both a full solve plus a live reasoning call), a
count question 2s, a refusal 2s.

### One scare that was not a regression

A full-suite run took **90 minutes** instead of the usual 140 seconds, and
another stalled at 33%. Neither reproduced: the suite splits into 38s
(integration) plus 94s (everything else), and every pairing was fast. Both slow
runs had been started while a browser harness and a dev server were competing
for the same cores — the MILP is CPU-bound and the assistant checks make live
LLM calls. Run alone, the suite is 139.90s against a 130.95s baseline. Worth
recording because "the tests got 35× slower" is the kind of signal that must be
chased to a cause rather than re-run until it looks fine.

---

## 7. Still not done

* **The account store is self-contained.** PBKDF2-HMAC-SHA256 at 240k
  iterations with per-user salts, which is sound, but a deployment should front
  it with a real identity provider. There is no password reset, no MFA, and no
  account lockout.
* **Sessions are bearer tokens in `localStorage`**, which is XSS-reachable. The
  application escapes what it renders, but an httpOnly cookie would be the
  stronger design.
* **SQLite is single-writer.** Correct and durable for one server; a
  multi-instance deployment needs Postgres. The store is a thin seam, so that is
  a swap rather than a rewrite.
* **Execution traces are not persisted** — stated above, and deliberate.
* **The LLM call budget is 4 per gateway instance** against a shared daily
  allowance. Fine for one analyst, not sized for concurrent users.
* **Facility-level REI still reports a negative performance impact** for F004
  and F007. Real, not a solver artefact — closing them lowers business cost
  while stranding more demand, because the shortage penalty is excluded from
  that measure by design. The engine logs it; nothing on screen explains it.
* **Uploaded signals still do not influence the forecast**, and there is still
  no contract parser.
