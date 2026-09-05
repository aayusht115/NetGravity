# NetGravity — Operations

What has to be true for a deployment to be safe, and how to make it true. Where
something cannot be arranged from inside this repository, it says so and says
who has to arrange it.

---

## 1. Configuration for production

`NETGRAVITY_ENV=production` is not cosmetic. It turns on:

| | development | production |
|---|---|---|
| Session cookie `Secure` flag | off | **on** (never sent over plain HTTP) |
| `Strict-Transport-Security` | absent | **`max-age=31536000; includeSubDomains`** |
| CORS | open unless an allowlist is set | same-origin unless `NETGRAVITY_CORS_ORIGINS` is set |
| Database TLS | `sslmode=require` defaulted for a remote host | as development, **plus** `sslmode=disable`/`allow` to a remote host is **refused at start-up** |
| Password-reset delivery | `log` channel permitted | `log` **refused**; a real channel must be configured |
| Werkzeug debugger | opt-in via `NETGRAVITY_DEBUG=1` | never |

Minimum production environment:

```bash
NETGRAVITY_ENV=production
NETGRAVITY_DATABASE_URL=postgresql://user:pass@host:5432/netgravity?sslmode=verify-full
NETGRAVITY_PUBLIC_URL=https://netgravity.example.com
NETGRAVITY_RESET_DELIVERY=smtp          # or webhook
NETGRAVITY_SMTP_HOST=smtp.example.com
NETGRAVITY_SMTP_FROM=no-reply@example.com
TEXT_API_TOKEN=...                      # optional; without it the assistant is deterministic
```

`NETGRAVITY_PUBLIC_URL` matters more than it looks. Password-reset links are
built from it, and the fallback is the request's own `Host` header — which is
attacker-controllable. A poisoned Host turns "reset your password" into "send
me your reset token". Set it.

### Behind a proxy

`NETGRAVITY_TRUSTED_PROXY=1` makes the rate limiter honour `X-Forwarded-For`.
Set it **only** when a proxy you control overwrites that header. Left on
without one, any caller sets their own identity and the rate limit stops
existing.

### Run it with a real server

`run.py` uses Flask's development server, which is single-threaded-ish, has no
request queue, and says so on start-up. In production:

```bash
gunicorn --workers 4 --threads 2 --timeout 600 'app.backend.app:app'
```

The 600-second timeout is not arbitrary: a KPI request can be waiting on a MILP
solve, and a shorter one kills the worker mid-solve.

---

## 2. The database

### Schema changes

The schema is a numbered, ordered list in
`app/backend/services/migrations.py`, applied once each and recorded in
`schema_migrations`. Adding a change means appending a `Migration` — never
editing or renumbering one that has shipped.

Migrations run automatically at start-up, and the application **refuses to
serve** against a schema whose version it does not recognise. On a fleet,
that means: deploy the new build to one instance, let it migrate, then roll the
rest. Migrations are forward-only by design — a rollback of a schema change
that has already accepted writes is data loss with a reassuring name.

```
python -c "from app.backend.services.persistence import database; \
           print(database.kind, database.schema_version)"
```

### Backups

```bash
# nightly
python scripts/backup_database.py --out /var/backups/netgravity --keep 30

# weekly, and after every schema change
python scripts/backup_database.py --out /var/backups/netgravity --verify-restore
```

`--verify-restore` restores the dump into a scratch database and compares every
table's row count. Run it on a schedule, not once. An unverified backup is a
belief.

Requires `pg_dump`, `pg_restore` and `psql` on PATH (or
`NETGRAVITY_PG_DUMP` / `NETGRAVITY_PG_RESTORE` / `NETGRAVITY_PSQL`).

### Point-in-time recovery

A dump restores one instant — whichever instant it was taken at. PITR needs a
**physical** base backup plus archived WAL, and `scripts/pitr.py` does all three
parts of it:

```bash
# Is archiving on at all?
python scripts/pitr.py status

# Turn it on. Writes postgresql.auto.conf over a normal superuser connection.
python scripts/pitr.py configure --data-dir /var/lib/postgresql/16/main
#   -> reports which settings need a RESTART. It never restarts anything.

# The physical backup WAL is replayed onto. A pg_dump cannot be used for this.
python scripts/pitr.py basebackup --out /backups/base-$(date +%F)

# Prove a recovery works. This is the part documentation cannot do.
python scripts/pitr.py drill
```

`drill` is the one that matters. It restores a base backup into a scratch
cluster, replays the archive to a chosen instant, starts it on another port, and
checks **both** directions: that a transaction committed before the target
survived, and that one committed after it did **not**. A recovery that restores
everything including the mistake has recovered nothing; one that restores
neither has lost committed data. Only checking both tests recovery rather than
testing that the server starts.

Verified on PostgreSQL 16.4: `PITR VERIFIED — a recovery to a chosen instant
keeps everything committed before it and nothing after it.`

**What is still your decision: where the archive goes.** The default
`archive_command` copies WAL to a local directory, which protects against
everything except losing the machine — and losing the machine is the main thing
a backup is for. In production, ship it off the box:

```bash
python scripts/pitr.py configure --data-dir /var/lib/postgresql/16/main   --archive-command 'aws s3 cp %p s3://backups/wal/%f --only-show-errors'
```

* **Managed** (RDS, Cloud SQL, Neon, Supabase): turn on automated backups and
  PITR in the provider's console instead. That is the recommended route, and
  `pitr.py status` will tell you whether it is actually on.
* **Self-hosted at scale**: `pgBackRest` or `WAL-G` do retention, compression
  and parallel restore, which this script deliberately does not.

Whichever you choose, run `pitr.py drill` on a schedule and write down the
recovery point objective you have actually achieved. "We have backups" is not
one, and neither is "PITR is configured".

### Migrating an existing SQLite store

```bash
python scripts/migrate_to_postgres.py \
    --sqlite data/netgravity.db \
    --postgres "postgresql://user:pass@host:5432/netgravity?sslmode=require"
```

Idempotent, non-destructive, and it re-reads every document from PostgreSQL
afterwards and compares it byte-for-byte against the source.

---

## 3. Identity

Self-contained, and honest about which parts of an identity system it is:

| | provided |
|---|---|
| Password hashing | PBKDF2-HMAC-SHA256, 240k iterations, per-user salt |
| Minimum length | 12 characters, plus a common-password refusal |
| Brute force | 10 failures in 15 minutes locks the account for 15, counted **in the database** |
| Session storage | httpOnly + SameSite=Lax cookie; `Secure` in production |
| CSRF | SameSite plus a double-submit token on unsafe methods |
| Session lifetime | 8-hour idle timeout that slides, **7-day absolute deadline that does not** |
| Password reset | single-use, 30-minute, rate-limited; only the token HASH is stored |
| Second factor | TOTP (RFC 6238) with 10 single-use recovery codes |
| Session management | list live sessions, sign out everywhere |
| Single sign-on | OpenID Connect, authorization code + PKCE |
| **Not provided** | SAML, SCIM provisioning, directory sync (deprovisioning), password history, admin-initiated resets |

### What the sign-up form adds, and what it does not

The form is stricter than the server, and only in that direction. It shows a
live checklist — 12 characters, an upper-case letter, a lower-case letter, a
digit, a symbol — and refuses to submit until every item is met, so nothing it
accepts can come back rejected for its shape. The server's own floor is
unchanged and remains the authority: length, plus the common-password refusal.
Composition rules are the form's advice, not a server policy; a password
created through the API or by an administrator is held only to the floor.

The form also accepts only `@kearney.com` addresses for **account creation**.
This is a guard on the form. `POST /api/auth/signup` still accepts any
syntactically valid address, so it stops a person filling in the wrong address,
not a client calling the API directly. Sign-in and password reset are
deliberately **not** restricted: an account can also arrive through single
sign-on or an administrator, and refusing those a way in — or a way to
recover — would lock out a user the server is willing to authenticate. If the
restriction needs to be a rule rather than a prompt, it belongs in
`SecurityService.signup` beside `validate_password_strength`.

### Single sign-on (OIDC)

Off unless configured. `/api/auth/oidc/providers` reports whether it is on, and
the sign-in screen shows a button only when it is — a button that leads to an
error page teaches a user the application is broken rather than that the feature
is off.

```bash
NETGRAVITY_OIDC_ISSUER=https://login.example.com
NETGRAVITY_OIDC_CLIENT_ID=...
NETGRAVITY_OIDC_CLIENT_SECRET=...        # omit for a public client; PKCE is always on
NETGRAVITY_OIDC_REDIRECT_URI=https://app.example.com/api/auth/oidc/callback
NETGRAVITY_OIDC_SCOPES="openid email profile"
NETGRAVITY_OIDC_PROVIDER_NAME="Company SSO"
NETGRAVITY_OIDC_ALLOWED_DOMAINS=example.com
NETGRAVITY_OIDC_AUTO_PROVISION=0          # 1 to create accounts on first sign-in
```

Decisions you are inheriting, each of which matters:

* **Accounts are linked on `(issuer, subject)`, never on e-mail alone.** An
  e-mail address is an assertion a provider makes; the subject is the identity
  it is making the assertion about. E-mail is used only to attach a first
  federated sign-in to an EXISTING local account, and only when the provider
  says it is **verified** and it is inside `ALLOWED_DOMAINS` if you set one.
* **`AUTO_PROVISION` is off by default.** A configured provider will usually
  authenticate a whole directory, and being authenticated is not the same as
  being invited. With it off, an unknown user is told to ask for an invitation.
* **PKCE with S256 always**, even with a client secret.
* **`state` and `nonce` are server-side and single-use.** A `state` in a cookie
  the client also controls is not a CSRF defence.
* **A federated account has no password.** There is nothing to guess, reset or
  leak. Unlinking the last provider from such an account is refused, because it
  would lock its owner out.

**Deprovisioning is still yours.** Removing someone from the directory stops
them signing in again; it does not end a session already issued here. Run
"sign out everywhere" for a leaver, or set a shorter absolute session deadline.
There is no SCIM endpoint.

ID tokens are verified against the provider's JWKS with an explicit asymmetric
algorithm allowlist — `alg: none` and every HMAC algorithm are refused before
any key is looked up, which is what closes the algorithm-confusion attack. See
`netgravity/tests/integration/test_oidc_sso.py`; most of it is the attack cases.

### Reset delivery

`NETGRAVITY_RESET_DELIVERY` selects the channel:

* `log` — writes the link to the application log. Development only; **refused
  in production**, because a reset link in a log file is a credential in a log
  file.
* `webhook` — POSTs to `NETGRAVITY_RESET_WEBHOOK`, optionally bearer-authenticated
  with `NETGRAVITY_RESET_WEBHOOK_TOKEN`. For a queue or a notification service.
* `smtp` — `NETGRAVITY_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM`,
  STARTTLS on by default.

`/api/status` reports the channel and whether it is actually configured.

---

## 4. Rate limits

| bucket | limit | override |
|---|---|---|
| `auth.login`, `auth.mfa` | 20 / 5 min | `NETGRAVITY_RATELIMIT_AUTH_LOGIN` |
| `auth.signup` | 60 / hour | `NETGRAVITY_RATELIMIT_AUTH_SIGNUP` |
| `auth.reset` | 10 / hour, and 5 / hour per account | `NETGRAVITY_RATELIMIT_AUTH_RESET` |
| `auth.oidc_start`, `auth.oidc_callback` | 30 / 5 min | `NETGRAVITY_RATELIMIT_AUTH_OIDC_START` |
| `scenario.simulate` | 30 / 5 min (two MILP solves each) | `NETGRAVITY_RATELIMIT_SCENARIO_SIMULATE` |
| `kpi.read` | 240 / min | `NETGRAVITY_RATELIMIT_KPI_READ` |
| `insights.read` | 120 / min | `NETGRAVITY_RATELIMIT_INSIGHTS_READ` |

Counters are **shared across every worker**, held in `rate_limit_windows` and
incremented by one atomic upsert. They used to be per process, which meant N
workers gave one caller N budgets — so the number a deployment advertised was
never the number it enforced, and the gap widened by exactly the factor you
scaled out to survive the load in the first place.

Two consequences worth knowing:

* **The written number is now the real number.** `auth.signup` was raised from
  10 to 60 per hour for this reason: at 10, a team behind one office NAT could
  register ten people an hour and no more.
* **A restart no longer resets a window.** That is correct for a rate limit and
  surprising the first time a test harness hits it. `/api/status` reports
  `storage.rate_limit_counters` as `shared` or `per-process`.

If the database is unreachable the limiter degrades to a per-process window
rather than to no limit at all. A degraded limit is still a limit; an outage of
the counter store must not become an open door.

---

## 5. What an upload is checked against

Everything on the mapping-review screen comes from one call,
`POST /api/ingestions/preview/upload-and-parse`, and nothing on it is
authored. The columns, the sample values, the row counts, the four totals and
the per-sheet verdicts are all that call's response; the screen is a rendering
of it, not a second opinion about it.

**Sheets are identified by their columns, not their names.** A sheet called
`Sheet1` is read correctly and one called `Facilities` whose headers are not
recognised is not. The sheet's role then decides what each of its columns
means — `Capacity_Units` is a facility's capacity on a facilities sheet and a
lane's on a lanes sheet — which is why the review screen tabs by sheet and
states the role it inferred for each.

Three states, and they are different facts:

| Status | What it means |
|---|---|
| **Used** | Recognised on an identified sheet; reaches a calculation |
| **Needs review** | The sheet's role could not be determined, so the field is the best match across every role — a guess, and it is used as suggested unless changed |
| **Not used** | Parsed, then read by nothing in this build |

"Not used" is common and is not an error: a real workbook carries operational
columns this build has no engine for. The summary states the share explicitly
("67 of 147 columns reach a calculation in this build") rather than reporting a
share of the ones it liked.

**Only tabular files are parsed.** The uploader accepts `.pdf` because
contracts and rate cards belong to the project record, but this build has no
contract parser, the PDF review screen says so on its face, and no term shown
there reaches the optimiser. PDFs are therefore not sent to the table parser at
all. A file the parser cannot read is named in `parse_errors` and reported on
its own card; it does not stop the other files in the same upload from being
read. A request in which nothing is readable still fails, with 422.

**Row-level quality is measured too, and is a separate question** from the
mapping: that one asks whether the columns were read correctly, this one
whether the rows can be trusted. The parser reports how many records it could
not use, duplicate and empty rows, and columns that are mostly blank. On the
review screen this is one chip on the file card — `10 records need attention ·
3 issues` — which opens the full list. It was a full-width card until it was
found to push the primary action off a 768px screen; the measurements are
unchanged, only their place on the page. Nothing here is repaired for you:
fixing a finding means editing your own file and uploading it again.

The screen is deliberately **one screenful**. The mapping table scrolls inside
its own box and the footer does not move, so `Confirm mapping & continue` is
reachable without scrolling past 147 rows you have already decided not to
change.

Nothing an upload contains is committed by this screen. `Confirm mapping &
continue` is what writes the network; until it is pressed, Back is lossless.

---

## 6. What the model does and does not do

The MILP is **multi-period**. `multi_period_policy` decides what a demand table
stating several periods is solved as:

* `FULL_HORIZON` (default) — every period modelled. Flow, demand and capacity
  are indexed by period, and stock may be carried between periods at facilities
  that can hold it. Costs are **horizon totals**: fixed and handling costs are
  charged in every period a facility is open, while opening, closure and capex
  are charged once.
* `PEAK` — collapsed to the largest period. Still the cheapest way to ask "can
  the footprint carry the worst month", and a one-period model at any horizon.
* `REPRESENTATIVE_MEAN` — collapsed to the mean.
* `SUM` — every period added together. Only correct if the periods are meant to
  be served simultaneously.

Whatever is chosen, the result carries `period_report` — the periods found, the
policy applied, `modelled_periods`, the per-period totals — and the solver
warnings say so. Nothing is collapsed or expanded silently.

**Cost of the horizon.** Measured on the bundled fixture
(`validation/phase_10_9/measure_horizon_scaling.py`): 24 periods is 23× the
variables and 7× the wall time of one period, and solves in 0.21 s. A collapse
policy stays a one-period model at any horizon. Measure a client network an
order of magnitude larger before trusting the worker timeout against it.

**What the horizon model does not do.** A plant cannot build ahead: in this
formulation a plant's outbound flow IS its production, so separating "made in
March" from "shipped in June" would need a production variable distinct from the
shipment variable. Pre-build therefore happens downstream, at a DC, where
inbound and outbound genuinely are two different flows. Cross-docks hold no
stock, by definition. Facility opening is one decision for the whole horizon,
not a phased build.

**Storage is bounded by what you state.** `FacilityRecord.storage_capacity_units`
caps carried stock. Left unset, stock is bounded by total horizon demand — an
unstated warehouse size does not become an unlimited one, and no capacity is
invented. Carrying stock is priced from the product's own `unit_value` and
`holding_rate`; a product with no stated value carries **no** holding cost, and
the solve says so in a warning rather than assuming a carrying rate.

**Contractual site commitments are enforced.** A lease or minimum-term clause
read out of an uploaded contract sets `contract_status` and
`contract_allows_early_closure` on the facility, and constraint C5c then pins
that site open. A scenario that closes it is reported INFEASIBLE with check
V-015 naming the conflict, rather than costed as though it were a decision the
client could take. A stated early-exit penalty becomes `closure_cost` and is
charged once when the model closes the site.

Silence is never read as a lock-in. A contract that does not say a site cannot
be exited leaves it closable, and a commitment naming a facility the network
does not contain is reported rather than force-matched — the likeliest cause is
that the network and the contracts name the same building differently, and that
is for a person to resolve. See `netgravity/ingestion/contracts_to_network.py`.

A facility can show a **negative** performance impact in a resilience
assessment: losing it makes the network cheaper. That is a finding, not a bug —
the baseline pins the client's footprint open, so a site whose fixed cost
exceeds its routing benefit shows a saving when removed. The engine writes a
diagnostic, the KPI layer carries it, and the assistant says it.

---

## 7. Health

`GET /api/status` is public and carries no customer data. It reports:

* `storage.engine` — `postgresql` or `sqlite`, resolved before anything mounts,
  and present even when the rest of the application failed to start because of it
* `durability` — row counts per store, and the schema version
* `orchestrator.llm_available`
* `reset_delivery` — the channel and whether it is configured

A deployment is misconfigured if `storage.engine` is `sqlite`, or
`reset_delivery.configured` is false, and the endpoint says so.
