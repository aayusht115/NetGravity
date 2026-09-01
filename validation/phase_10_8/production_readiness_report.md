# Phase 10.8 — closing the production blockers

The list this phase was given was my own, from the end of Phase 10.7:

> The account store is self-contained — no reset, no MFA, no lockout. Tokens
> live in `localStorage`, which is XSS-reachable. PostgreSQL is now the store,
> but nothing operates it: no backup, no PITR, no enforced TLS, no migration
> versioning. The MILP aggregates all demand into one period. F004/F007 REI
> still reports a negative impact, real and unexplained. One dead module,
> `graph.js`, still holds a hardcoded 18-node prototype network.

Every item is closed below, with what was actually wrong and what it does now.
Where a control genuinely cannot be arranged from inside a repository — PITR is
the only one — it says so and says who arranges it.

---

## 1. The database was a store, not an operated system

### Schema changes had no mechanism

The schema was one block of `CREATE TABLE IF NOT EXISTS`. That creates a schema
on an empty database and does nothing on a populated one — so the first column
ever added to a table with rows in it would have had to be added by hand, on
every deployment, forever. The schema had already changed once (`analyses`).
This phase needed to add six tables and four columns; there was no way to
express that.

`app/backend/services/migrations.py` is now a numbered, ordered list applied
once each and recorded in `schema_migrations`, with each migration's statements
and its version row committing in **one transaction** — so a failure part-way
leaves the version unrecorded and the migration retried, never skipped.
Migration 1 is the previous schema, written with `IF NOT EXISTS`, so an
existing populated database adopts the migration table without its data being
touched. The application **refuses to serve** against a schema version it does
not recognise.

Verified against a real pre-migration database with rows in it: `v0 → v3`, the
user and the session preserved, the new columns added, and a second open
applying nothing.

There are no down-migrations, deliberately. A rollback of a schema change that
has already accepted writes is data loss with a reassuring name.

### The connection was unencrypted by default

The URL carries the password in the clear and every row of a client's network
crosses it. Left alone, libpq negotiates TLS only if the server offers it and
**silently falls back to plaintext** — so "we use Postgres" said nothing about
whether anything was encrypted.

Now: a URL that states an `sslmode` is respected; one that does not gets
`sslmode=require` appended; and in production, `sslmode=disable` or `allow` to
a non-local host is **refused at start-up** rather than warned about. A warning
in a log is not a control. A local socket is exempt, because TLS to 127.0.0.1
protects against nothing and would stop the platform running on a laptop.

### There were no backups

`scripts/backup_database.py` takes one — `pg_dump -Fc` for PostgreSQL, the
SQLite online backup API otherwise (not a file copy, which can capture a torn
write mid-WAL) — and with `--verify-restore` **restores it into a scratch
database and compares every table's row count**. An unverified backup is a
belief.

Run against the live store: 171 rows across 13 tables dumped and restored with
every count matching.

Writing it surfaced a defect in the persistence layer itself:
`Database(path=...)` read `url or configured_database_url()`, so it silently
opened the configured PostgreSQL instead of the file it was handed whenever the
environment named one. The caller that names a specific database — a restore
verification, a migration source — is exactly the one that could not get it.

### PITR is still not provided, and cannot be from here

Continuous WAL archiving is configured on the **server** (`archive_mode`, an
`archive_command`, or a managed provider's own setting). A nightly dump gives a
recovery point of up to 24 hours. `docs/operations.md` says how to do better
and who has to do it. This is the one item on the list that a repository cannot
close.

---

## 2. The account store was a credential store

| | before | now |
|---|---|---|
| Minimum password | 8 characters | 12, plus a common-password refusal |
| Brute force | **nothing** | 10 failures in 15 min locks for 15, counted in the database |
| Password reset | **nothing** | single-use, 30-minute, rate-limited; only the token HASH stored |
| Second factor | **nothing** | TOTP (RFC 6238) + 10 single-use recovery codes |
| Session lifetime | 8-hour idle only | 8-hour idle **plus a 7-day absolute deadline** |
| Session management | **nothing** | list live sessions, sign out everywhere |
| Token storage | `localStorage` | httpOnly + SameSite cookie |

Details that matter:

**The lockout is a row, not a dictionary.** A lock held in process memory
resets on restart and is invisible to a second web worker. Failures on an
*unknown* address are counted too — otherwise an attacker enumerating
addresses is throttled on the ones that exist and unthrottled on the ones that
do not, which is itself an oracle.

**The reset endpoint answers identically** for a known address, an unknown one
and a rate-limited one. Anything else turns it into an account-enumeration
oracle. Only the token's hash is stored, so a database read is not an account
takeover, and redeeming one revokes every existing session — a reset is what
someone does when they think another person has their credentials.

**The TOTP is verified against RFC 6238's own test vectors** (T=59, 1111111109,
1234567890, 2000000000 — all four match), and it reports *which time step* a
code verified against so the step can be claimed atomically. A TOTP
implementation returning only True/False cannot express the thing that has to
be enforced: a captured six-digit code stays usable for the rest of its
30-second window unless the step is spent.

**Enrolment is not active until confirmed** with a working code — a mis-scanned
QR that activated immediately would lock a user out of their own account. The
secret and the recovery codes are shown once and are not readable back out.

**A second factor makes sign-in two steps.** The first returns a short-lived
*challenge* carrying a different token prefix, and `resolve_session` refuses it
everywhere — so a client that ignores `mfa_required` gets nothing usable rather
than a bypass.

### The session left `localStorage`

Any script on the page could read it, so one XSS anywhere exfiltrated a
credential valid for eight hours, on any machine, with no further access. It is
now an httpOnly cookie the browser attaches on its own; the client holds
nothing, and clears any token left over from the old scheme rather than using
it.

A cookie is sent automatically, which is what CSRF exploits, so:
`SameSite=Lax` (the primary control), plus a double-submit token echoed in
`X-CSRF-Token`. Required only when the request authenticated **via the
cookie** — a bearer header cannot be set cross-site without a CORS preflight
this server does not grant, so that path is not ridable and requiring a token
there would break API clients for nothing.

An explicit bearer wins over the cookie, both because a deliberate credential
should get the identity it asked for and because it keeps the CSRF rule aligned
with what is actually ridable.

---

## 3. XSS had no mitigation, only a moved prize

Moving the token out of `localStorage` removes what an injection can *steal*.
It does nothing about the injection. The application shipped with **no security
headers at all**.

The Content Security Policy is now `script-src 'self'` with no
`'unsafe-inline'` and no `'unsafe-eval'`, plus `connect-src 'self'` (an
exfiltration becomes a blocked request), `frame-ancestors 'none'`,
`object-src 'none'`, nosniff, a Referrer-Policy, a Permissions-Policy, HSTS in
production, and `Cache-Control: no-store` on every API response.

Making that policy true required real work, because a policy that has to be
loosened to fit the page is not a policy:

* **Four libraries came from three CDNs** — Leaflet, Chart.js, Three.js and
  OrbitControls, from unpkg, jsDelivr and cdnjs. A script tag pointed at
  someone else's server is a supply chain, on a page that renders client
  network data. All four are vendored under `app/frontend/vendor/`.
* **Inter was an `@import` from Google Fonts**, which leaks every visitor's IP
  and user agent on every page load and fails offline. Vendored, seven faces.
* **One inline `<script>` block** became `js/landing-bootstrap.js`.
* **Thirty-five inline `onclick` attributes** became `data-action` attributes
  dispatched through an allowlist in `js/actions.js`. Inline handlers cannot be
  nonced — nonces apply to `<script>` elements, not to event-handler
  attributes — so this was not optional. The allowlist is also stricter than
  what it replaced: `window[el.dataset.action]()` would reach any global,
  while `ACTIONS` is the complete, reviewable set of things a click may do.
* The **inlined single-file build** is no longer served from the application's
  origin. It concatenates every module into inline script and can never satisfy
  this policy; it is a separate deliverable from
  `scripts/build_standalone.py`.

Measured in a browser against the running application: **0 CSP violations, 0
external requests, 0 page errors**, with Leaflet, Chart.js, Three.js and
OrbitControls all loaded and the `data-action` dispatch working.

---

## 4. Rate limiting

Two endpoint families, two reasons. Credential endpoints are guessed at — the
account lockout handles a sustained attack on *one* account, and this handles
the other shape, a spray across many that never trips a per-account counter.
Solve endpoints are expensive: one caller could occupy every worker with MILP
solves and the platform stops answering for everyone, no malice required.

Counters are in process memory, which is stated rather than hidden: with N
workers a caller gets N budgets. That is the right trade for a single-process
deployment, and `RateLimiter` is one class so changing the backing store is one
change.

---

## 5. A multi-period demand table crashed the solver

Not "was aggregated". **Crashed.**

```
pulp.constants.PulpError: overlapping constraint names: demand_MKT_P1
```

The MILP has no period index on its flow variables and its demand constraint
was named `demand_{market}_{product}`, so two rows for the same market and
product in different periods produced two constraints with one name and PuLP
refused to build the model. Twelve months of demand is the shape most planning
data arrives in.

Constraint names now carry the period, and demand is collapsed to one
representative period by `OptimizationConfig.multi_period_policy`:

* `REPRESENTATIVE_MEAN` (default) — the average. Correct against per-period
  capacity: 100 units in each of two periods against 150 of capacity is
  comfortably feasible, and `SUM` would make it 200 against 150 and report a
  shortage that does not exist.
* `PEAK` — the largest period. The capacity-planning question.
* `SUM` — every period added, for data meant to be served simultaneously.

Nothing is silent. The result carries `period_report` — the periods found, the
policy applied, the per-period totals — the solver warnings repeat it, and the
default policy's note names the peak it did **not** size for. A single-period
network is returned unchanged and pays nothing.

Rows are grouped by market, product **and SLA**: two rows under different
service levels are different commitments, and averaging across them would
invent a service level the client never stated.

---

## 6. The negative REI, and a narrative that could delete itself

A facility whose loss makes the network *cheaper* is a real finding: the
baseline pins the client's footprint open, so a site whose fixed cost exceeds
its routing benefit shows a saving when removed. The engine had always written
a full diagnostic saying so — and it had always stopped at the log. Every
consumer of the KPI got a figure that reads as a bug in the software rather
than a finding about the network.

The diagnostic now travels in the KPI's `input_evidence`, and the assistant
states it in one sentence when it reports one.

Writing that sentence found a worse defect. `ReasoningResult.narrative` is
capped at 700 characters and the template joined its sentences **unbounded** —
so a network with enough to say about it produced a string that failed
validation, failed the whole `reasoning.synthesise` capability, and returned an
**empty summary**. Losing every sentence because there was one too many is the
worst available handling of a length limit. The join is now bounded, drops from
the end, and says how many it dropped.

---

## 7. Dead code

`app/frontend/js/graph.js` held a hardcoded 18-node prototype network and was
imported by nothing. Deleted.

---

## Validation

| suite | result |
|---|---|
| Backend regression | **2,607 passed · 4 skipped** |
| `netgravity/tests/integration/test_security_hardening.py` (new) | **47/47** |
| `netgravity/tests/test_multi_period.py` (new) | **15/15** |
| `validation/phase_10_8/run_production_readiness_check.py` (new) | **39/39** |
| `validation/phase_10_7/run_postgres_check.py` | 19/19 |
| `validation/phase_10_7/run_greenfield_and_hardcoded_check.py` | 26/26 |
| `validation/phase_10_6/run_persistence_check.py` | 14/14 |
| `validation/phase_10_6/run_identity_map_chat_check.py` | 21/21 |
| `validation/phase_10_5/run_scenario_types_check.py` | 15/15 |
| `validation/phase_10_5/run_scenario_ui_check.py` | 29/29 |
| `validation/phase_10_4/run_scenario_check.py` | 11/11 |
| `validation/phase_10_3/run_ui_flow_check.py` | 25/25 |
| `validation/phase_10_3/run_empty_project_check.py` | 10/10 |
| `validation/phase_10_1/run_client_data_e2e.py` | 27/27 |

The harnesses caught two regressions this phase introduced, which is what they
are for:

* **`hasSession` read the session cookie** — which is httpOnly, and therefore
  invisible to script, so it was always false. `restoreSession()` returned
  early on every page load and a refresh dropped a signed-in user to the
  landing page with a perfectly valid session in their browser. It reads the
  CSRF cookie, which is set alongside the session and IS readable by design.
  Caught by U-26; pinned by a test that asserts the marker is the readable
  cookie and not the httpOnly one.
* **The reasoning narrative could delete itself** — see §6. Caught by the
  backend suite.

Two harnesses needed updating, and both changes are strengthenings rather than
relaxations:

* **I-05** checked the `alert()` the profile menu used to open. The menu now
  opens the account security screen, so the check reads that screen — and
  additionally requires the two-factor controls to be on it.
* **A-01** made its "anonymous" request with a bare `fetch`, which now attaches
  the httpOnly session cookie automatically. It uses `credentials: 'omit'`, so
  the request is genuinely anonymous again. Left alone it would have quietly
  stopped testing anything.

---

## What is still not true

Stated plainly, because a list of fixes is not a certificate.

* **Point-in-time recovery is not configured.** A nightly verified dump gives a
  recovery point of up to 24 hours. Better needs WAL archiving on the server or
  a managed instance, and `docs/operations.md` says how.
* **No SSO, SCIM or directory integration.** This is a complete standalone
  account system, not a replacement for an identity provider. An organisation
  that has one should front this with it.
* **Rate-limit counters are per process.** Correct for one worker, approximate
  for several. A shared counter needs Redis.
* **The MILP is single-period.** Multi-period input is now handled, reported
  and explained — not modelled. Inventory carried between periods, and
  seasonality as a design variable, are a different model.
* **Execution traces are still not persisted**, deliberately. A restart keeps
  the answers and loses the workings.
* **`/api/kpis/facilities/<id>` returns no resilience or risk block** for the
  baseline workflow, because `NETWORK_STATE_QUERY` does not run an REI
  assessment. The endpoint reports what the execution produced.
* **Uploaded external signals are still not routed into the forecast**, and the
  card says so rather than implying they were.
* **There is no contract parser.**
* **No load test has been run.** The rate limits and the gunicorn timeout in
  `docs/operations.md` are reasoned, not measured.

Tests passing is not the same as production-ready (§39). What the numbers above
establish is that the controls named in the previous report now exist, are
enforced, and are verified against a real PostgreSQL server and a real browser.
