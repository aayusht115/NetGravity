# Phase 10.9 — the MILP, the insights, and the rest of my own list

Two asks. The first was the list I closed the last phase with, under "What is
still not true". The second arrived during the work: *"insights are not visible
for the uploaded data set"*.

The second turned out to be the more interesting one, so it is first.

---

## 1. Insights were not visible, and there were almost none to see

Two separate faults, and only one of them was the wiring.

### Nothing fetched them

`POST /orchestrator/insights` existed and worked. `reasoning-service.js` existed
and wrapped it. The Reasoning Agent existed and produced grounded briefings.

And **no line of code connected them.** `HOME_INSIGHTS` and
`HOME_ACTION_ITEMS` — the two structures the Home feed reads — were initialised
empty, read by the feed and the deep dive, and written by nothing at all. They
were only ever *cleared*. So every user who uploaded their own data saw

> No insights have been generated for this network yet.

permanently, on a network that had been fully solved.

The orchestrator endpoint could not have closed this on its own: it is keyed by
Digital Twin `state_id`, and a dashboard holds a `project_id`. Resolving one to
the other means knowing that a project has a snapshot, that a snapshot has a twin
state, and which of several is the one the KPIs came from — control-plane
knowledge that has no business being in a browser. `GET /api/insights` answers
the question the screen actually has, and is cached in the same durable analysis
store the KPIs use, so a briefing costs one reasoning pass per network version
rather than one per request.

### And there was almost nothing to fetch

The deterministic template emitted a `KPIInsight` for exactly two themes — Cost
and Scenario impact — so a solved baseline network produced **one** insight, "I
see the current cost position clearly", whatever the network said. An overloaded
DC, a missed SLA and stranded demand all reached the reader as one cost card.

The evidence for all of them was already in the payload and already narrated in
prose. Nothing was being made of it. Seven further themes now exist alongside
Cost and Scenario impact, each emitted only when its metric is present:

| theme | fires on | severity |
|---|---|---|
| Service | unserved demand, or fill rate, or an SLA miss | RISK / INFORMATION |
| Capacity | any open site at or above the 90% threshold | RISK |
| Utilisation | two or more sites at or below 30% | OPPORTUNITY |
| Cost structure | the largest component of the solver's own breakdown | INFORMATION |
| Footprint | candidate sites the plan does not use; sites whose loss lowers cost | OPPORTUNITY |
| Resilience | the highest single-site REI exposure | RISK |
| Carbon | emissions, where any were computed | INFORMATION |

**Severity is stated by the engine.** The Home feed had been deciding a card's
colour, icon and priority by searching its prose for the strings `"high impact"`,
`"opportunity"` and `"positive"` — so the rendering of a finding depended on
incidental wording, and any finding phrased differently was shown as a neutral
"Status" however serious it was. `InsightSeverity` is now set where the finding
is made.

### The recommendation was not a recommendation

Every branch produced the same sentence — *"I recommend reviewing the quantified
impact above before moving to a formal option appraisal"* — whether the network
stranded a fifth of its demand or ran comfortably. It is now chosen by the
evidence, ordered by what a planner has to deal with first: an infeasible model,
then unserved demand, then a site at its capacity threshold, then a footprint
that costs more than it saves, then idle capacity, then nothing.

No branch states a saving. Naming the next test is a recommendation; naming its
result would be an invention, because no scenario has been run. A test asserts
that no branch emits a currency figure.

### What the deep dive had been going to show

Wiring the feed up made `insight-detail.js` reachable for the first time. It was
836 lines annotated "PROTOTYPE / MOCKED", and it presented as ordinary page
content: a seven-month "Actual / Projected" utilisation chart generated from a
hash of the insight's id; a before/after cost table whose delta came from
`synth(id, 'cost', 6, 18)`; a service level of `94.6%` as a literal; an "amount
at risk" of ₹8–32L, likewise hashed; an editable "shift 8–18% of volume" slider
recomputing all of it; a drafted e-mail to "Priya Mehta (Regional Planning
Analyst)", who does not exist; and Approve / Reject buttons that changed their
own label and nothing else.

None of it had ever been seen, because nothing populated the feed. All of it is
gone rather than relabelled — a caveat under a fabricated chart does not make the
chart true, and a hashed rupee figure beside a real one is indistinguishable to a
reader. The page now shows the finding, its full narrative, **the metrics it
cites with their authoritative values and which engine computed them**, the
recommendation, and two ways to act: test a change as a scenario, or open the
site in the twin. The scenario button is the honest version of the shift slider —
a planner who wants to know what moving 12% of volume costs can have the MILP
answer it.

The "₹NNL/month at risk" line came off the Home feed for the same reason.

### Two grounding defects this exposed

**A cited threshold was read as a measurement.** "No open site reaches the 90%
threshold" was adjudicated CONTRADICTED against `pct_demand_in_sla = 100` — a
percentage measured on something else entirely — and the figure was stripped out
mid-sentence. Thresholds now travel with the evidence, sourced from the module
that owns them. And because they are percentages, a further rule was needed: a
threshold may be **cited** but is never the value a wrong claim is compared
against, or an invented "cost rose 12%" comes back CONTRADICTED by a utilisation
threshold and the distinction between "misreported a real figure" and "invented
one" is destroyed.

**Every cost-reducing scenario failed grounding.** The narrative states a
direction in words and a magnitude in digits — "the scenario DECREASES business
cost by 8,506,746.48" — while the fact holds −8,506,746.48. They did not match,
the nearest same-kind currency fact was picked instead, and the claim was
reported as CONTRADICTED. The whole briefing was marked GROUNDING_FAILED, its
confidence dropped to LOW, and the figure was stripped from the sentence — for
the outcome a planner is looking for. Found only because I made the log line name
the claims instead of counting them: `contradicted=4` is not a diagnostic.

---

## 2. The MILP is multi-period

Last phase stopped a crash (`PulpError: overlapping constraint names`) and then
collapsed every period into one, saying so loudly. That is honest and it is not a
multi-period model. A network that can carry the mean of twelve months and not
the peak of one is a different network, and no averaging policy can tell you
which one you have.

`FULL_HORIZON` is now the default. Flow, demand, capacity and stock are indexed
by period:

```
x_{ijvkt} ≥ 0     flow on arc (i,j,v,k) in period t
I_{ikt}   ≥ 0     stock at facility i of product k at the END of t
u_{jkt}   ≥ 0     shortage in period t

(C1) Σ x_{ijvkt} + u_{jkt} = D_{jkt}                 ∀j,k,t
(C2) Σ x_{ijvkt} ≤ Cap_i y_i                          ∀i,t
(C4) I_{ik,t−1} + Σ_in x = Σ_out x + I_{ikt}          ∀i ∈ DCs, k, t
(C9) Σ_k I_{ikt} ≤ S_i y_i                            ∀i,t
```

### What this changes that a collapse cannot

Demand of 100, 100, 160 against a plant that makes 120 a period. Over the
horizon the plant can make 360 and demand is 360, so every unit is servable —
but only by producing ahead and holding stock. Measured:

```
plant out by period   [(1, 120.0), (2, 120.0), (3, 120.0)]
DC out by period      [(1, 100.0), (2, 100.0), (3, 160.0)]
stock held            DC end of p1: 20   end of p2: 40
served                360.0 of 360.0
```

A single-period model cannot express that at all. It either reports a 40-unit
shortfall or averages the peak out of existence.

### Costs over a horizon

* Fixed and handling costs are charged in **every** period a facility is open.
  Charging a per-period rent once would make a twelve-month plan look like it
  rents a warehouse for one month, and would tilt every siting decision towards
  more facilities than the money supports.
* Opening, closure and capex stay **one-time**.
* Holding cost comes from the product's own `unit_value` and `holding_rate`. A
  product with no stated value carries **no** holding cost — and the solve says
  so in a warning rather than assuming a carrying rate, because free storage is a
  real property of that plan.
* `capacity_units` on a facility decision is the horizon capacity, so its ratio
  with the horizon throughput is a real utilization; `peak_utilization_pct` is
  reported separately, because a DC at 50% for eleven months and 140% in December
  is not a DC at 57%.

### What the horizon model does not do, stated

A plant cannot build ahead: in this formulation a plant's outbound flow **is** its
production, so separating "made in March" from "shipped in June" needs a
production variable distinct from the shipment variable. Pre-build happens
downstream at a DC, where inbound and outbound genuinely are two different flows.
Cross-docks hold no stock, by definition. Facility opening is one decision for the
whole horizon, not a phased build.

### The cost of the horizon, measured

`validation/phase_10_9/measure_horizon_scaling.py`, on the bundled fixture:

| periods | policy | variables | constraints | seconds |
|---|---|---|---|---|
| 1 | FULL_HORIZON | 54 | 27 | 0.029 |
| 3 | FULL_HORIZON | 163 | 82 | 0.067 |
| 6 | FULL_HORIZON | 319 | 157 | 0.133 |
| 12 | FULL_HORIZON | 631 | 307 | 0.111 |
| 24 | FULL_HORIZON | 1,255 | 607 | 0.208 |
| 24 | PEAK | 54 | 27 | 0.013 |

**24 periods is 23× the variables and 7× the time** — sub-linear, and 0.21 s
against a documented 300 s worker timeout. A collapse policy stays a one-period
model at any horizon, which is why `PEAK` is kept: it is still the cheapest way
to ask "can the footprint carry the worst month".

Stock rows are zero in that table, and that is correct: this fixture's plants can
meet its peak, so there is nothing to gain by pre-building. Stock is carried when
it is needed and not otherwise.

### The whole forecast now reaches the solver

`apply_forecast_to_network` took ONE period from a forecast produced over six to
twenty-four. The other eleven months of a twelve-month forecast were computed,
returned to the screen, and dropped on the way to the MILP — which is exactly the
seasonality the forecast exists to describe.
`apply_forecast_horizon_to_network` applies the whole horizon, through one shared
implementation so the coverage rules cannot drift. A pair the forecast covers for
only part of the horizon is refused, not mixed: a demand table that is forecast in
March and observed in April is one nobody could attribute.

### A silent defect found on the way

`FacilityDecision` was constructed with `fixed_cost_period=`, `status=`,
`latitude=` and `longitude=` — **none of which are fields.** Pydantic's default
`extra="ignore"` dropped all four without a word, which means `fixed_cost`,
`total_facility_cost`, `inventory_cost` and `n_markets_served` were `0.0` on
every facility of every result this system has ever produced, and the coordinates
the map needed never travelled. Fixed, and the decision models now use
`extra="forbid"` so a misspelled keyword fails at the call site instead of
producing a plausible, empty record.

`NetworkKPIs.total_cost` also omitted opening, closure and holding costs, so a
plan that opened a candidate reported a total that was not the number the solver
minimised — and the reconciliation check could not see it, because it compared
the same incomplete sum on both sides.

---

## 3. Point-in-time recovery

The last report said this "cannot be closed from a repository". That was half
right and it let the wrong conclusion stand. `archive_mode` is a server setting;
three other things are not, and none had been done:

1. **Configure it.** `ALTER SYSTEM SET` over a superuser connection, reporting
   which settings need a restart — and never restarting anything, because that
   is an operator's decision.
2. **Take the backup PITR needs.** A `pg_dump` is logical; you cannot replay WAL
   onto it. The backup script took only the dump, so even with archiving on there
   was nothing to recover *from*. `pitr.py basebackup` takes the physical one.
3. **Prove a recovery works.** `pitr.py drill` restores a base backup into a
   scratch cluster, replays the archive to a chosen instant, starts it on another
   port, and checks **both** directions.

```
[1/6] base backup
[2/6] write a row that must survive the recovery
[3/6] recovery target = 2026-09-01 13:06:49.976856+05:30
[4/6] write the row that represents the mistake
[5/6] recover the base backup to the target instant
[6/6] verify what the recovered cluster holds
      rows recovered: [1]

  committed row before the target recovered : YES
  row after the target correctly excluded   : YES

  PITR VERIFIED.
```

Verified against PostgreSQL 16.4. Both directions matter: a recovery that
restores everything including the mistake has recovered nothing, and one that
restores neither has lost committed data.

Getting there found three real faults, all of which present as something else:

* **`copy` on Windows refuses forward slashes.** Verified directly — the same
  copy succeeds with backslashes and fails with forward slashes, on the same
  file. Which meant the config-file escaping had to be right rather than
  sidestepped, and PostgreSQL reports the consequence as *"recovery ended before
  configured recovery target was reached"* — which reads as a WAL problem. The
  archive was complete and correct the whole time.
* **`pg_ctl start` with a captured stdout never returns.** The postmaster
  inherits the pipe and holds it open, so the call blocks until timeout even
  though the server came up in a second. It made a successful drill look like a
  hung one, twice.
* **`configure` reported success while `archive_mode` was still off**, because
  it compared requested values against `SHOW`, which humanises `60` to `1min`,
  and read `pending_restart` in a race with the reload it had just signalled.

What remains yours: **where the archive goes.** The default copies WAL to a local
directory, which protects against everything except losing the machine — and
losing the machine is the main thing a backup is for. `--archive-command` takes
whatever you decide.

---

## 4. Rate-limit counters are shared

They were in process memory, and that was stated as a known limit: with N
workers a caller got N budgets. The trouble is which way it fails. The limit
exists to stop one caller occupying every worker with MILP solves — and the
moment you add workers to survive that load, the limit loosens by exactly the
factor you added. The number a deployment advertised was never the number it
enforced, and the gap grew with the deployment.

One row per (bucket, client), incremented by one atomic upsert. Verified: four
threads hammering one bucket consumed **25 of 40** requests against a limit of
25 — one budget, not four.

Redis is not used, deliberately: a second datastore to run, secure, back up and
monitor, for a table with one small row per active caller, when PostgreSQL is
already all four of those things. If the store is unreachable the limiter
degrades to a per-process window rather than to no limit at all.

Two consequences, both real:

* **The written number is now the real number**, so `auth.signup` was raised from
  10 to 60 per hour: at 10, a team behind one office NAT could register ten
  people an hour and no more. Every bucket is now overridable per deployment.
* **A restart no longer resets a window.** Correct for a rate limit, and it broke
  a validation harness that had been creating accounts across runs — which is how
  I found it. The harnesses now clear the window explicitly rather than having
  the control disabled for them.

---

## 5. Execution traces survive the process

A 500-entry ring buffer in memory was the whole store, which meant the answers
outlived the workings: a KPI survived a restart because it was persisted, while
the record of which capability produced it, from which snapshot, under which
governance verdict, did not. It also meant the 501st execution silently evicted
the first. Both are the wrong way round for the one record whose entire purpose
is to be readable long afterwards.

Sealed traces are written through to `execution_traces`, and `get()` reads back
on a miss. `ExecutionTrace.from_dict` is the exact inverse of `to_dict`, pinned
by a round-trip test. Writes are guarded: a trace that cannot be stored is
logged, and the execution still returns its answer. Retention is 90 days.

---

## 6. The facility endpoint's resilience block

`/api/kpis/facilities/<id>` promised resilience and risk in its own docstring and
returned neither, because `NETWORK_STATE_QUERY` — the workflow the KPI endpoints
run — does not assess resilience. The blocks were dropped silently, which reads
as "this facility carries no exposure": the one conclusion the absence of an
assessment cannot support.

`?include=resilience` now runs the assessment and caches it as its own analysis
variant. It is opt-in because REI re-solves the network once per facility — a
network with eight sites is nine MILP solves, and putting that behind every
dashboard load would multiply the wait by the size of the footprint. Without it,
the response says `resilience_status: NOT_REQUESTED` with the reason, rather than
omitting the blocks.

---

## 7. Uploaded signals reach the forecast

Market-intelligence signals were parsed, stored and displayed, and that was all.
The router (`routing/signal_router.py`) and the enricher
(`forecasting/signals/enrichment.py`) were both complete, both tested, and
reachable only by a caller that constructed a request by hand. Every screen
showed a forecast that had never seen the market intelligence sitting in the same
upload.

They are now attached to the forecast request and routed through the
orchestrator's own rules — nothing is bypassed: the router still decides what may
inform a forecast, on confidence, guardrail verdict and whether a signal names an
entity this network contains. The response carries both `attached` and
`series_adjusted`, because "3 signals attached, 0 adjustments applied" is a state
a reader has to be able to see.

---

## 8. Contractual site commitments

My last report said "there is no contract parser". That was too broad: there is
one, and it reads freight rate cards into cost rules. The real gap was narrower
and worse.

Constraint C5c pins `y_i = 1` for a facility under an active contract that
forbids early closure. Validation check V-015 names the conflict when a scenario
closes one anyway. The Digital Twin reports `contract_status`;
`metrics/contracts.py` summarises it.

**And nothing had ever set those fields.** No ingestion path, no API, no scenario
override — `contract_status` defaulted to NONE on every facility of every
network. The enforcement was structurally present and permanently inert, so a
plan could recommend closing a site the client was contractually unable to close,
and the one part of the system whose job was to object had no way to know.

Lease and minimum-term clauses are now extracted as `FacilityCommitment` and
applied to the network. Measured, end to end, for the first time:

```
DC_NORTH_NEW open with the contract applied: True
objective without contract: 150,627.70
objective with contract   : 181,915.36
validation errors on a forced closure: ['V-015']
forced-closure solve status: INFEASIBLE
```

**Silence is never read as a lock-in.** A contract that does not say a site
cannot be exited leaves it closable; an unstated term would otherwise block a
closure the client is free to make, on the strength of a clause nobody wrote.
`allows_early_closure` is `False` only where the document says so, `None` is kept
distinct from `False`, an end date alone is not a lock-in, and no exit penalty is
derived from a rent and a remaining term. A commitment naming a facility the
network does not contain is reported rather than force-matched — nothing is
fuzzy-matched, because the cost of binding the wrong building is a plan that
cannot be executed.

---

## 9. Single sign-on

The account store here is complete and it is still the wrong place for an
organisation's identity to live. Joiners and leavers are managed in a directory,
and an account that survives someone's departure because nobody thought to remove
it from a supply-chain tool is the ordinary way access outlives employment.

OpenID Connect, authorization code with PKCE. Off unless configured, and the
sign-in screen shows a button only when `/api/auth/oidc/providers` says one can
work — a button that leads to an error page teaches a user the application is
broken rather than that the feature is off.

No JWT library is installed here and `cryptography` is, so the ID token verifier
is written against it. Hand-written JWT verification is where token bugs live, so
most of its test module is the attack cases:

| attack | refused because |
|---|---|
| `alg: none` | the algorithm allowlist is checked **before** any key lookup |
| HS256 signed with the provider's public key | no HMAC algorithm is ever allowed |
| `jwk`/`jku`/`x5c`/`x5u` in the header | key material comes from the issuer's JWKS or nowhere |
| unknown `kid`, or a different key's signature | must match a published key |
| a token minted for another client | `aud`, and `azp` when present |
| an expired, future-dated, or very old token | `exp`, `iat`, `nbf`, and a max age independent of `exp` |
| a token replayed from another session | `nonce`, single-use and server-side |
| a discovery document naming another issuer | refused, or the `iss` check becomes a check against the attacker's value |
| metadata over plain HTTP | the provider's metadata is the trust anchor |

**Accounts are linked on `(issuer, subject)`, never on e-mail alone.** An e-mail
is an assertion a provider makes; the subject is the identity it is making the
assertion about. E-mail attaches a first federated sign-in to an existing local
account only when the provider says it is **verified** and it is inside
`ALLOWED_DOMAINS`. Auto-provisioning is **off** by default: a configured provider
usually authenticates a whole directory, and being authenticated is not the same
as being invited.

**Deprovisioning is still not solved.** Removing someone from the directory stops
them signing in again; it does not end a session already issued here. There is no
SCIM endpoint, and `docs/operations.md` says so under the feature rather than
somewhere else.

---

## 10. Load, measured

The last report was explicit that the rate limits and the worker timeout were
"reasoned, not measured". `scripts/load_test.py` produces the measurement.

**Realistic usage** — 8 concurrent users, 1 s think time, 41 s:

| endpoint | requests | p50 | p90 | p99 | 429 | 5xx |
|---|---|---|---|---|---|---|
| `/api/status` | 64 | 16.6 ms | 26.9 | 28.1 | 0 | 0 |
| `/api/projects` | 64 | 12.2 | 26.6 | 29.3 | 0 | 0 |
| `/api/kpis/network` | 64 | 19.8 | 29.2 | 31.4 | 0 | 0 |
| `/api/kpis/facilities` | 64 | 15.8 | 28.4 | 30.0 | 0 | 0 |
| `/api/insights` | 63 | 7.6 | 27.4 | 30.3 | 0 | 0 |

7.8 requests/second sustained, **worst p99 31 ms**, nothing refused, nothing
limited.

**Flood** — the same 8 clients with no think time, 40 s: 21,897 requests offered,
**397/second completed**, worst p99 35 ms, **0 server errors**, 9,008 refused by
the rate limit and 6,000 (27%) refused by the server as work it had no capacity
to accept.

Two things this measurement is careful about, because a measurement presented
wrongly is worse than none:

* **It says which server it measured.** `Werkzeug/3.1.8` — Flask's development
  server, not what production runs. The latencies are a floor for a gunicorn
  deployment, not a prediction of it.
* **It distinguishes a refused connection from a 5xx.** 27% refused is a capacity
  finding about the server; 0 server errors means the application did not fault.
  Reporting them together as "failures" would have hidden that.
* **It says when the analysis was served from the store rather than solved.** An
  earlier run printed "first analysis: 31 ms" and called it a solve. The response
  now carries `compute_seconds` — how long the solve took when it ran — so a
  cached answer still reports the real cost of the analysis.

---

## Validation

| suite | result |
|---|---|
| Backend regression | **2,750 passed · 4 skipped** |
| `netgravity/tests/test_multi_period_horizon.py` (new) | **24/24** |
| `netgravity/tests/test_contract_commitments.py` (new) | **16/16** |
| `netgravity/tests/integration/test_insight_generation.py` (new) | **25/25** |
| `netgravity/tests/integration/test_oidc_sso.py` (new) | **61/61** |
| `netgravity/tests/integration/test_operational_hardening.py` (new) | **15/15** |
| `validation/phase_10_9/run_insights_ui_check.py` (new, browser) | **21/21** |
| `validation/phase_10_9/run_sso_ui_check.py` (new, browser) | **10/10** |
| `scripts/pitr.py drill` against PostgreSQL 16.4 | **PITR VERIFIED** |
| `scripts/backup_database.py --verify-restore` | 131 rows, every count matching |
| `validation/phase_10_9/measure_horizon_scaling.py` | 13 solves, 1→24 periods |
| `scripts/load_test.py` | 2 profiles, 0 server errors |
| All six new suites together | **156 passed** |

Every earlier harness re-run and green: scenario UI 29/29, client E2E 27/27, UI
flow 25/25, empty project 10/10, scenario check 11/11, scenario types 15/15,
identity/map/chat 21/21, greenfield+hardcoded 26/26, Postgres 19/19, persistence
14/14, production readiness 39/39.

### Regressions I introduced, and what caught them

* **Duplicate insight cards.** A facility briefing restates network-level themes,
  so the feed showed "I see the current cost position clearly" twice out of nine
  cards. Caught by the browser harness reading the rendered titles; de-duplicated
  by headline, and pinned by a check.
* **Every cost-reducing scenario failed grounding.** §1. Caught by making the log
  line name its claims.
* **A cited threshold contradicted a measurement.** §1. Caught by the backend
  suite the moment thresholds became citable.

Two harnesses needed a change, and both are strengthenings:

* The harnesses that create accounts now **clear** the shared rate-limit window
  at start-up. The control stays in the request path; what changed is that they
  no longer inherit the previous run's budget. Its behaviour is asserted in
  `test_operational_hardening.py` instead of implicitly by these.
* `run_insights_ui_check.py` opens a project the way the application does —
  marking it active and reloading, so `restoreSession()` → `loadProjects()` →
  `openProjectById()` runs. Calling `openProjectById` directly returns `false`
  when the app's own project list has not loaded, which is how an earlier version
  of this harness measured an empty dashboard no user would ever see.

**The loading screen went up one round trip too late.** Not mine, and found
because check L-02 in the greenfield harness failed twice in a row rather than
once. `beginAnalysisLoading()` was called inside `getReadiness().then()`, so
between `enterApp()` revealing the shell and the overlay appearing there was a
real window — one HTTP round trip — in which the dashboard was visible with no
figures behind it. That is precisely what the loading screen exists to prevent,
and it is why the check samples every 80 ms. Readiness only ever affected the
overlay's *wording*, so the overlay is now raised first and the wording revised
when readiness arrives. 26/26 twice.

That distinction is worth keeping: the same check had failed earlier under CPU
contention from a parallel harness and passed on a clean re-run, which is
flakiness. Failing twice cleanly is a defect.

Deleted: `app/frontend/js/integration/services/reasoning-service.js`. It wrapped
`/orchestrator/insights`, which `insight-service.js` now supersedes, plus two
methods with no caller. Nothing imported it.

Also fixed: `/api/status` reported its mounted blueprints from a **hardcoded
list** which was already wrong — it named `ingestion_preview`, which is not a
blueprint on this app, and omitted every blueprint added after it was written. A
new blueprint answering 404 looked, on the endpoint whose job is to report what
is mounted, exactly like one that was working. It now reads `app.blueprints`.

---

## What is still not true

* **No SCIM or directory sync.** SSO authenticates; it does not deprovision.
  Removing a leaver from the directory stops future sign-ins and does not end a
  session already issued. Run "sign out everywhere", or shorten the absolute
  session deadline.
* **No SAML.** OIDC only.
* **The load measurement is of the development server.** Werkzeug, not gunicorn,
  because gunicorn does not run on this platform. The latencies are a floor.
* **The solve-scaling measurement is one fixture on one machine.** 24 periods in
  0.21 s says the horizon model is affordable here; it does not say what a client
  network an order of magnitude larger costs. Measure before trusting the 300 s
  timeout against one.
* **A plant cannot build ahead**, cross-docks hold no stock, and facility opening
  is not phased across the horizon. §2.
* **PITR ships WAL to a local directory by default.** That protects against
  everything except losing the machine. `--archive-command` is the seam.
* **The ID token verifier is hand-written.** It is tested against the attacks
  that matter, and a maintained library is still better than a correct one you
  have to keep correct. Prefer PyJWT or Authlib if you are adding dependencies.
* **Contract extraction depends on a model reading a document.** Every clause
  carries its `source_excerpt` and its confidence for exactly that reason. A
  commitment that pins a site open should be read by a person before a plan is
  built on it.
* **Insight themes are the nine named above** — the seven new ones plus Cost and
  Scenario impact — and a network briefing shows at most six of them. A finding
  outside that set is not produced, so an empty feed is honest about this
  engine rather than complete about the network.

Tests passing is not the same as production-ready (§39). What the numbers above
establish is that each item on the previous report's own list has been closed by
a change with a measurement attached, and that four defects the work exposed —
the dropped `FacilityDecision` fields, the incomplete `total_cost`, the signed
cost-delta grounding failure, and the stale blueprint list — were latent long
before this phase and are fixed.
