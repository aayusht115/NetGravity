"""
NetGravity — Schema migrations
==============================
The schema is a numbered, ordered list of migrations, applied once each and
recorded in `schema_migrations`.

Why this replaced `CREATE TABLE IF NOT EXISTS`
----------------------------------------------
That idiom creates a schema on an empty database and does nothing on a
populated one — which is exactly the wrong behaviour the moment a table needs a
new column. There is no way to express "add `mfa_secret` to `users`" with it, so
the second schema change would have been made by hand on every deployment, or
not at all. The schema had already changed once (the `analyses` table); a
second change to a table with rows in it needs this.

Properties
----------
* **Ordered and recorded.** Each migration has an integer version applied in
  ascending order, and its version is written to `schema_migrations` in the
  SAME transaction as its statements. A migration cannot be half-applied and
  believed complete.
* **Idempotent at the boundary.** Migration 1 is the schema as it stood before
  this module existed, written with `IF NOT EXISTS`, so an already-populated
  database adopts the migration table without its data being touched.
* **Forward only.** There are no down-migrations. A rollback of a schema change
  that has already accepted writes is not a reversal; it is data loss with a
  reassuring name. Roll forward, or restore from a backup taken beforehand.
* **Two dialects, one list.** PostgreSQL and SQLite differ in three places — the
  timestamp type, the placeholder token, and how a script is executed. Each
  migration renders itself per dialect rather than being written twice.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, List, NamedTuple

logger = logging.getLogger(__name__)


class Migration(NamedTuple):
    version: int
    name: str
    #: dialect ("postgres" | "sqlite") -> the statements to run, in order.
    statements: Callable[[str], List[str]]


def _ts(dialect: str) -> str:
    return "DOUBLE PRECISION" if dialect == "postgres" else "REAL"


def _m001_baseline(dialect: str) -> List[str]:
    """The schema as it stood before migrations existed."""
    ts = _ts(dialect)
    return [
        f"""CREATE TABLE IF NOT EXISTS users (
            user_id     TEXT PRIMARY KEY,
            email       TEXT NOT NULL UNIQUE,
            document    TEXT NOT NULL,
            created_at  {ts} NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            expires_at  {ts} NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id)",
        f"""CREATE TABLE IF NOT EXISTS projects (
            project_id  TEXT PRIMARY KEY,
            owner_id    TEXT NOT NULL,
            document    TEXT NOT NULL,
            updated_at  {ts} NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_projects_owner ON projects(owner_id)",
        f"""CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id  TEXT PRIMARY KEY,
            network_id   TEXT NOT NULL,
            document     TEXT NOT NULL,
            created_at   {ts} NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_snapshots_network ON snapshots(network_id)",
        f"""CREATE TABLE IF NOT EXISTS scenario_networks (
            scenario_id  TEXT PRIMARY KEY,
            snapshot_id  TEXT NOT NULL,
            document     TEXT NOT NULL,
            created_at   {ts} NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS scenarios (
            scenario_id  TEXT PRIMARY KEY,
            project_id   TEXT NOT NULL,
            document     TEXT NOT NULL,
            created_at   {ts} NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_scenarios_project ON scenarios(project_id)",
        """CREATE TABLE IF NOT EXISTS network_data (
            kind        TEXT NOT NULL,
            network_id  TEXT NOT NULL,
            document    TEXT NOT NULL,
            PRIMARY KEY (kind, network_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS analyses (
            snapshot_id   TEXT PRIMARY KEY,
            data_version  TEXT NOT NULL,
            document      TEXT NOT NULL,
            computed_at   {ts} NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS app_state (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        )""",
    ]


def _m002_auth_hardening(dialect: str) -> List[str]:
    """
    Everything the account store needs to stop being a demo.

    * `login_attempts` records failures per identity so a password can be
      rate-limited and an account locked. Held in the database rather than in
      process memory, because a lockout that resets when the server restarts —
      or that only one of two web workers knows about — is not a lockout.
    * `password_resets` holds single-use, expiring reset tokens. Only the HASH
      of the token is stored, so a database read does not hand an attacker the
      ability to take over every account with a reset in flight.
    * `sessions` gains the fields that make a session auditable and revocable:
      when it was created, when it was last seen, an absolute deadline
      independent of idle extension, and the client it was issued to.
    """
    ts = _ts(dialect)
    return [
        f"""CREATE TABLE IF NOT EXISTS login_attempts (
            identity     TEXT PRIMARY KEY,
            failures     INTEGER NOT NULL DEFAULT 0,
            first_failed {ts} NOT NULL,
            last_failed  {ts} NOT NULL,
            locked_until {ts}
        )""",
        f"""CREATE TABLE IF NOT EXISTS password_resets (
            token_hash  TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            created_at  {ts} NOT NULL,
            expires_at  {ts} NOT NULL,
            used_at     {ts}
        )""",
        "CREATE INDEX IF NOT EXISTS ix_password_resets_user ON password_resets(user_id)",
        f"ALTER TABLE sessions ADD COLUMN created_at {ts}",
        f"ALTER TABLE sessions ADD COLUMN last_seen_at {ts}",
        f"ALTER TABLE sessions ADD COLUMN absolute_expiry {ts}",
        "ALTER TABLE sessions ADD COLUMN client TEXT",
    ]


def _m003_mfa(dialect: str) -> List[str]:
    """
    Second-factor enrolment, stored beside the account.

    A dedicated table rather than fields inside the `users` document: the
    secret and the recovery codes are credentials in their own right, they are
    written on a different schedule from the profile, and keeping them out of
    the document means an accidental `public()`-style projection of the user
    record cannot carry them.
    """
    ts = _ts(dialect)
    return [
        f"""CREATE TABLE IF NOT EXISTS mfa_enrolments (
            user_id        TEXT PRIMARY KEY,
            secret         TEXT NOT NULL,
            confirmed_at   {ts},
            created_at     {ts} NOT NULL,
            last_used_step INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
            user_id    TEXT NOT NULL,
            code_hash  TEXT NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, code_hash)
        )""",
    ]


def _m004_shared_rate_limits(dialect: str) -> List[str]:
    """
    Rate-limit counters that every worker can see.

    Held in process memory before this, which meant N workers gave one caller N
    budgets — so the limit a deployment advertised was not the limit it
    enforced, and the gap widened exactly as you scaled out to handle load.

    One row per (bucket, client), updated in place by a single atomic
    upsert, so a burst arriving simultaneously on four workers is counted once
    each rather than four times in isolation.
    """
    ts = _ts(dialect)
    return [
        f"""CREATE TABLE IF NOT EXISTS rate_limit_windows (
            bucket        TEXT NOT NULL,
            client        TEXT NOT NULL,
            window_start  {ts} NOT NULL,
            hits          INTEGER NOT NULL,
            PRIMARY KEY (bucket, client)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_rate_limit_window_start "
        "ON rate_limit_windows(window_start)",
    ]


def _m005_execution_traces(dialect: str) -> List[str]:
    """
    Orchestrator execution traces, kept across a restart.

    Traces lived only in memory, so every restart kept the answers and lost the
    workings: a KPI could still be read, but the record of which capability
    produced it, from which snapshot, with which warnings, was gone. That is
    the record an audit asks for, and it was the one thing guaranteed not to
    survive.

    `document` is the serialised trace; the columns beside it are the ones
    worth querying without parsing it.
    """
    ts = _ts(dialect)
    return [
        f"""CREATE TABLE IF NOT EXISTS execution_traces (
            execution_id  TEXT PRIMARY KEY,
            actor_id      TEXT,
            intent        TEXT,
            workflow      TEXT,
            snapshot_id   TEXT,
            status        TEXT,
            started_at    {ts} NOT NULL,
            document      TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_execution_traces_started "
        "ON execution_traces(started_at)",
        "CREATE INDEX IF NOT EXISTS ix_execution_traces_actor "
        "ON execution_traces(actor_id)",
        "CREATE INDEX IF NOT EXISTS ix_execution_traces_snapshot "
        "ON execution_traces(snapshot_id)",
    ]


def _m006_federated_identity(dialect: str) -> List[str]:
    """
    The link between a local account and an identity provider subject.

    Kept in its own table rather than as a field on the user document because
    one account may be reachable through more than one provider, and because
    the (issuer, subject) pair is the thing that must be UNIQUE — matching on
    e-mail alone lets any provider that will assert an address take over an
    account that already exists under it.
    """
    ts = _ts(dialect)
    return [
        f"""CREATE TABLE IF NOT EXISTS federated_identities (
            issuer      TEXT NOT NULL,
            subject     TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            email       TEXT,
            created_at  {ts} NOT NULL,
            last_seen   {ts},
            PRIMARY KEY (issuer, subject)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_federated_identities_user "
        "ON federated_identities(user_id)",
    ]


#: Ordered. Append only; never renumber, never edit one that has shipped.
MIGRATIONS: List[Migration] = [
    Migration(1, "baseline_schema", _m001_baseline),
    Migration(2, "auth_hardening", _m002_auth_hardening),
    Migration(3, "mfa", _m003_mfa),
    Migration(4, "shared_rate_limits", _m004_shared_rate_limits),
    Migration(5, "execution_traces", _m005_execution_traces),
    Migration(6, "federated_identity", _m006_federated_identity),
]

SCHEMA_VERSION = MIGRATIONS[-1].version


def _migration_table(dialect: str) -> str:
    return (
        f"""CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at {_ts(dialect)} NOT NULL
        )"""
    )


def apply_migrations(backend: Any) -> List[int]:
    """
    Bring `backend` up to `SCHEMA_VERSION`. Returns the versions applied now.

    `backend` supplies `dialect`, `execute(sql, params)`, `query(sql, params)`
    and `atomic(statements, record)`. Each migration's statements and its
    `schema_migrations` row commit together, so a failure part-way leaves the
    version unrecorded and the migration is retried on the next start rather
    than being skipped.
    """
    dialect = backend.dialect
    placeholder = "%s" if dialect == "postgres" else "?"

    backend.execute(_migration_table(dialect), ())
    rows = backend.query("SELECT version FROM schema_migrations", ())
    applied = {int(r["version"]) for r in rows}

    done: List[int] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        statements = migration.statements(dialect)
        record = (
            f"INSERT INTO schema_migrations(version, name, applied_at) "
            f"VALUES({placeholder}, {placeholder}, {placeholder})",
            (migration.version, migration.name, time.time()),
        )
        backend.atomic(statements, record)
        done.append(migration.version)
        logger.info("persistence.migration.applied version=%d name=%s",
                    migration.version, migration.name)

    if done:
        logger.info("persistence.schema.migrated to=%d applied=%s",
                    SCHEMA_VERSION, done)
    return done


def current_version(backend: Any) -> int:
    """The highest applied migration version, or 0 on a fresh database."""
    try:
        rows = backend.query("SELECT version FROM schema_migrations", ())
    except Exception:  # noqa: BLE001 — table absent means nothing is applied
        return 0
    return max((int(r["version"]) for r in rows), default=0)
