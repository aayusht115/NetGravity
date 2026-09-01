"""
NetGravity — Durable storage
============================
Everything the application must not lose when the process restarts: accounts,
sessions, projects, uploaded networks, the analysis computed from them, and
solved scenarios.

PostgreSQL is the store this runs on. SQLite remains as a fallback so the
platform starts, and the 2,500-test suite runs, on a machine with no database
server — but a deployment that means to keep data sets `NETGRAVITY_DATABASE_URL`
and gets Postgres. `/api/status` names which one is actually in use, because
"is my work being kept, and where" is not a question anyone should have to
infer from a log line.

    NETGRAVITY_DATABASE_URL=postgresql://user:pass@host:5432/netgravity
    # or DATABASE_URL, the conventional name most platforms already set

Why Postgres
------------
SQLite has one writer. That is fine for one process and wrong for anything
else: two application instances behind a load balancer, or a background worker
alongside the web process, serialise behind a file lock and eventually collide.
Concurrent solves already hold write transactions open for tens of seconds.

Why the documents are TEXT and not JSONB
----------------------------------------
Every row is a JSON document under typed keys rather than a wide relational
schema: these records are Pydantic models that already know how to serialise
themselves, and re-describing their fields in DDL would create a second
definition of every one of them to keep in step by hand.

They are stored as TEXT rather than JSONB deliberately. JSONB is a normalised
representation — it reorders keys, drops duplicates, and re-renders numbers
through `numeric`. This store's contract is that an uploaded network comes back
exactly as it went in, and that property is asserted by the persistence check.
TEXT keeps one serialisation path with byte fidelity across both backends.
Nothing here queries inside a document; the columns that ARE queried are real
columns with real indexes.

What is deliberately NOT stored
-------------------------------
Execution contexts and audit traces. An execution is one in-flight run; the
artefacts it produces — snapshots, scenarios, KPI results — are persisted
individually, so a restart loses the workings and keeps the answers. Persisting
live `ExecutionContext` objects, which hold typed solver results, open futures
and step state, would be storing a process's stack.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/netgravity.db"

#: Environment variables that select PostgreSQL, in order of precedence.
_URL_ENV_VARS = ("NETGRAVITY_DATABASE_URL", "DATABASE_URL")


def configured_database_url() -> Optional[str]:
    """The PostgreSQL URL this process should use, or None for SQLite."""
    for var in _URL_ENV_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return None


#: Hosts for which an unencrypted connection is not a finding.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def enforce_transport_security(url: str, *, production: bool) -> str:
    """
    Refuse to send credentials and customer data over an unencrypted socket.

    The connection string carries the password in the clear and every row of a
    client's network crosses it. Left to itself libpq negotiates TLS only if
    the server offers it and will silently fall back to plaintext, so "we use
    Postgres" said nothing about whether anything was encrypted.

    Rules:
      * a URL that already states an `sslmode` is respected — the operator has
        decided, and a managed provider often pins its own;
      * otherwise `sslmode=require` is appended, so the default is encrypted;
      * in production, `sslmode=disable` or `allow` to a NON-LOCAL host is
        refused outright rather than warned about. A warning in a log is not a
        control.

    A local socket is exempt: TLS to 127.0.0.1 protects against nothing and
    would stop the platform running on a developer's machine.
    """
    lowered = url.lower()
    try:
        host = url.split("@", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    except IndexError:
        host = ""
    is_local = host in _LOCAL_HOSTS

    if "sslmode=" in lowered:
        weak = any(f"sslmode={mode}" in lowered for mode in ("disable", "allow"))
        if weak and production and not is_local:
            raise RuntimeError(
                f"NETGRAVITY_ENV=production refuses an unencrypted database "
                f"connection to '{host}'. The URL sets a weak sslmode; use "
                f"'require', 'verify-ca' or 'verify-full'."
            )
        return url

    if is_local:
        return url

    separator = "&" if "?" in url else "?"
    logger.info("persistence.tls.defaulted host=%s sslmode=require", host)
    return f"{url}{separator}sslmode=require"


# ---------------------------------------------------------------------------
# Schema
#
# The schema lives in `migrations.py` as a numbered, ordered list applied once
# each and recorded in `schema_migrations`. It used to be one block of
# `CREATE TABLE IF NOT EXISTS`, which creates a schema on an empty database and
# does nothing on a populated one — so the first column ever added to a table
# with rows in it would have had to be added by hand on every deployment.
#
# `_legacy_schema_statements` is retained ONLY as the definition migration 1
# adopts; nothing calls it at runtime.
# ---------------------------------------------------------------------------

def _legacy_schema_statements(dialect: str) -> List[str]:
    """
    The pre-migration schema, kept for reference only.

    Its statements now live in `migrations._m001_baseline`, which is what an
    existing database adopts as version 1.
    """
    from app.backend.services.migrations import _m001_baseline
    return _m001_baseline(dialect)


#: Every table the application owns, in dependency-free order. Used for
#: reporting on /api/status and for wiping a test database.
TABLES = ("users", "sessions", "projects", "snapshots", "scenario_networks",
          "scenarios", "network_data", "analyses", "app_state",
          "login_attempts", "password_resets", "mfa_enrolments",
          "mfa_recovery_codes")


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class _SQLiteBackend:
    """One connection per thread, WAL journaling, JSON documents as TEXT."""

    dialect = "sqlite"
    placeholder = "?"

    def __init__(self, path: str) -> None:
        self.path = path
        self.describe = path
        self._local = threading.local()
        self._is_memory = path == ":memory:"
        if not self._is_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # An in-memory database is per-connection, so it needs ONE shared
        # connection or each thread gets its own empty database. Tests use it.
        self._shared: Optional[sqlite3.Connection] = (
            self._connect() if self._is_memory else None)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        if not self._is_memory:
            # WAL lets readers proceed during a write, which matters because a
            # solve holds a request open for tens of seconds.
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def execute(self, sql: str, params: Tuple) -> int:
        cur = self._conn.execute(sql, params)
        return cur.rowcount or 0

    def query(self, sql: str, params: Tuple) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(sql, params)]

    def atomic(self, statements: List[str], record) -> None:
        """Run `statements` and `record` as one transaction, or none of them."""
        conn = self._conn
        conn.execute("BEGIN")
        try:
            for statement in statements:
                conn.execute(statement)
            conn.execute(record[0], record[1])
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        if self._shared is not None:
            self._shared.close()
            self._shared = None


class _PostgresBackend:
    """
    A pooled PostgreSQL connection.

    A pool rather than a connection per thread: Flask's thread count is not
    bounded by anything this module controls, and Postgres charges a real
    process per connection. `min_size=1` keeps start-up cheap on a machine
    where the database is only touched occasionally.
    """

    dialect = "postgres"
    placeholder = "%s"

    def __init__(self, url: str) -> None:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self._psycopg = psycopg
        self.url = url
        # Never log the URL: it carries the password.
        self.describe = _redact_url(url)
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=int(os.environ.get("NETGRAVITY_DB_POOL_MAX", "10")),
            kwargs={"row_factory": dict_row, "autocommit": True},
            timeout=30.0,
            open=False,
        )
        # A short wait on FIRST open, a long one thereafter. A misconfigured
        # host should be reported in seconds at start-up, not after half a
        # minute of an apparently-hung process; a busy pool during a solve is a
        # different situation and keeps the 30-second checkout timeout above.
        self._pool.open(wait=True, timeout=float(
            os.environ.get("NETGRAVITY_DB_CONNECT_TIMEOUT", "10")))

    def execute(self, sql: str, params: Tuple) -> int:
        with self._pool.connection() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount or 0

    def query(self, sql: str, params: Tuple) -> List[Dict[str, Any]]:
        with self._pool.connection() as conn:
            return list(conn.execute(sql, params))

    def atomic(self, statements: List[str], record) -> None:
        """
        Run `statements` and `record` as one transaction, or none of them.

        PostgreSQL has transactional DDL, so a migration that fails half way
        leaves the schema and the recorded version equally untouched.
        """
        with self._pool.connection() as conn:
            conn.autocommit = False
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(record[0], record[1])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True

    def close(self) -> None:
        self._pool.close()


def _redact_url(url: str) -> str:
    """`postgresql://user:secret@host:5432/db` -> `postgresql://user@host:5432/db`."""
    try:
        scheme, rest = url.split("://", 1)
        if "@" not in rest:
            return url
        credentials, host = rest.split("@", 1)
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}@{host}"
    except Exception:  # noqa: BLE001
        return "<database url>"


# ---------------------------------------------------------------------------

class Database:
    """
    The application's store, over PostgreSQL or SQLite.

    SQL is written once with a `?` placeholder and translated for the backend,
    so there is one query per operation rather than one per dialect. Only
    `INSERT ... ON CONFLICT ... DO UPDATE` syntax is relied on beyond plain SQL,
    and both backends implement it identically.
    """

    def __init__(self, path: Optional[str] = None, url: Optional[str] = None) -> None:
        # An EXPLICIT argument wins over the environment, either way round.
        #
        # This read `url or configured_database_url()`, so `Database(path=...)`
        # silently opened the configured PostgreSQL instead of the file it was
        # handed whenever the environment named one. The caller that asks for a
        # specific file — a restore verification, a migration source, a test —
        # is the one that most needs to get the database it named, and it was
        # the one that could not.
        if url is None and path is None:
            url = configured_database_url()
        self._write_lock = threading.RLock()
        if url:
            production = (os.environ.get("NETGRAVITY_ENV", "development")
                          .strip().lower() == "production")
            url = enforce_transport_security(url, production=production)
            try:
                self._backend: Any = _PostgresBackend(url)
                self.kind = "postgresql"
            except Exception as exc:  # noqa: BLE001
                # Falling back silently would be the worst outcome: the process
                # would run, look healthy, and write a user's work to a local
                # file nobody is backing up. It is refused loudly instead, and
                # the caller decides.
                raise RuntimeError(
                    f"PostgreSQL was configured ({_redact_url(url)}) but could not "
                    f"be reached: {type(exc).__name__}: {exc}"
                ) from exc
        else:
            self._backend = _SQLiteBackend(
                path or os.environ.get("NETGRAVITY_DB_PATH", DEFAULT_DB_PATH))
            self.kind = "sqlite"
        self.path = self._backend.describe
        from app.backend.services.migrations import (
            SCHEMA_VERSION, apply_migrations, current_version,
        )
        self.migrations_applied = apply_migrations(self._backend)
        self.schema_version = current_version(self._backend)
        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Schema is at version {self.schema_version} but this build "
                f"expects {SCHEMA_VERSION}. Refusing to serve against a schema "
                f"it does not understand."
            )

    # -- dialect plumbing ------------------------------------------------
    def _sql(self, sql: str) -> str:
        if self._backend.placeholder == "?":
            return sql
        return sql.replace("?", self._backend.placeholder)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._write_lock:
            return self._backend.execute(self._sql(sql), tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        return self._backend.query(self._sql(sql), tuple(params))

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def count(self, table: str) -> int:
        row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 — fixed names
        return int(row["n"]) if row else 0

    # -- documents -------------------------------------------------------
    @staticmethod
    def dumps(document: Dict[str, Any]) -> str:
        return json.dumps(document, default=str, separators=(",", ":"))

    @staticmethod
    def loads(raw: str) -> Dict[str, Any]:
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # A corrupt row is skipped rather than taking the process down on
            # start-up, and logged loudly, because silently losing a record is
            # worse than the crash it replaces.
            logger.error("persistence.row_unreadable len=%s", len(raw or ""))
            return {}

    def put_state(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO app_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, default=str)),
        )

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM app_state WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def close(self) -> None:
        self._backend.close()


#: Application-wide handle. Built on import so every store can bind to it.
database = Database()


@atexit.register
def _close_on_exit() -> None:
    """
    Return pooled connections before the interpreter finalises.

    Without this, psycopg's pool tries to join its worker threads from a
    `__del__` during shutdown and raises `PythonFinalizationError` — harmless,
    but it prints a traceback after a clean run, which is exactly the kind of
    noise that trains people to ignore tracebacks.
    """
    try:
        database.close()
    except Exception:  # noqa: BLE001 — shutdown must not raise
        pass


def reset_database(path: Optional[str] = None, url: Optional[str] = None) -> Database:
    """
    Point the application at a different database.

    For tests and for a deployment that configures the target after import.
    Module-level references keep working because the stores hold this object,
    not a copy of its state.
    """
    global database  # noqa: PLW0603 — one process-wide handle, by design
    database.close()
    database = Database(path, url)
    return database


# ---------------------------------------------------------------------------
# Typed accessors, one per kind of record
# ---------------------------------------------------------------------------

def save_user(user_id: str, email: str, document: Dict[str, Any],
              created_at: float) -> None:
    database.execute(
        "INSERT INTO users(user_id, email, document, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET email=excluded.email, "
        "document=excluded.document",
        (user_id, email, Database.dumps(document), created_at),
    )


def load_users() -> List[Dict[str, Any]]:
    return [Database.loads(r["document"])
            for r in database.query("SELECT document FROM users")]


def save_session(token: str, user_id: str, expires_at: float) -> None:
    """
    Write a session with only its idle expiry.

    Superseded by `save_session_record`, which also stores when the session was
    created, when it was last seen, its ABSOLUTE deadline and the client it was
    issued to. A row written through here has no absolute deadline, and
    `AuthService.load` derives one from `created_at` rather than reading the
    missing value as "expired in 1970".

    Kept because a caller outside this repository may still use it; nothing in
    the application does.
    """
    now = time.time()
    database.execute(
        "INSERT INTO sessions(token, user_id, expires_at, created_at, "
        "last_seen_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET expires_at=excluded.expires_at",
        (token, user_id, expires_at, now, now),
    )


def delete_session(token: str) -> None:
    database.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions(now: float) -> int:
    return database.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))


def load_sessions(now: float) -> List[Tuple[str, str, float]]:
    return [(r["token"], r["user_id"], r["expires_at"])
            for r in database.query(
                "SELECT token, user_id, expires_at FROM sessions WHERE expires_at >= ?",
                (now,))]


def save_project(project_id: str, owner_id: str, document: Dict[str, Any],
                 updated_at: float) -> None:
    database.execute(
        "INSERT INTO projects(project_id, owner_id, document, updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
        "owner_id=excluded.owner_id, document=excluded.document, "
        "updated_at=excluded.updated_at",
        (project_id, owner_id, Database.dumps(document), updated_at),
    )


def delete_project(project_id: str) -> None:
    database.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))


def load_projects() -> List[Dict[str, Any]]:
    return [Database.loads(r["document"])
            for r in database.query(
                "SELECT document FROM projects ORDER BY updated_at ASC")]


def save_snapshot(snapshot_id: str, network_id: str, document: Dict[str, Any],
                  created_at: float) -> None:
    database.execute(
        "INSERT INTO snapshots(snapshot_id, network_id, document, created_at) "
        "VALUES(?,?,?,?) ON CONFLICT(snapshot_id) DO UPDATE SET "
        "document=excluded.document",
        (snapshot_id, network_id, Database.dumps(document), created_at),
    )


def load_snapshots() -> List[Dict[str, Any]]:
    return [Database.loads(r["document"])
            for r in database.query(
                "SELECT document FROM snapshots ORDER BY created_at ASC")]


def save_scenario_network(scenario_id: str, snapshot_id: str,
                          document: Dict[str, Any], created_at: float) -> None:
    database.execute(
        "INSERT INTO scenario_networks(scenario_id, snapshot_id, document, created_at) "
        "VALUES(?,?,?,?) ON CONFLICT(scenario_id) DO UPDATE SET "
        "document=excluded.document",
        (scenario_id, snapshot_id, Database.dumps(document), created_at),
    )


def load_scenario_networks() -> List[Dict[str, Any]]:
    return [Database.loads(r["document"])
            for r in database.query(
                "SELECT document FROM scenario_networks ORDER BY created_at ASC")]


def save_scenario(scenario_id: str, project_id: str, document: Dict[str, Any],
                  created_at: float) -> None:
    database.execute(
        "INSERT INTO scenarios(scenario_id, project_id, document, created_at) "
        "VALUES(?,?,?,?) ON CONFLICT(scenario_id) DO UPDATE SET "
        "document=excluded.document",
        (scenario_id, project_id, Database.dumps(document), created_at),
    )


def delete_scenario(scenario_id: str) -> None:
    database.execute("DELETE FROM scenarios WHERE scenario_id = ?", (scenario_id,))


def load_scenarios() -> List[Tuple[str, Dict[str, Any]]]:
    return [(r["project_id"], Database.loads(r["document"]))
            for r in database.query(
                "SELECT project_id, document FROM scenarios ORDER BY created_at ASC")]


def save_network_data(kind: str, network_id: str, document: Any) -> None:
    database.execute(
        "INSERT INTO network_data(kind, network_id, document) VALUES(?,?,?) "
        "ON CONFLICT(kind, network_id) DO UPDATE SET document=excluded.document",
        (kind, network_id, json.dumps(document, default=str)),
    )


def load_network_data(kind: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for row in database.query(
            "SELECT network_id, document FROM network_data WHERE kind = ?", (kind,)):
        try:
            out[row["network_id"]] = json.loads(row["document"])
        except (TypeError, ValueError):
            logger.error("persistence.network_data_unreadable kind=%s", kind)
    return out


# -- Analysis --------------------------------------------------------------

def save_analysis(snapshot_id: str, data_version: str, document: Dict[str, Any],
                  computed_at: float) -> None:
    """Store the authoritative analysis computed from one network snapshot."""
    database.execute(
        "INSERT INTO analyses(snapshot_id, data_version, document, computed_at) "
        "VALUES(?,?,?,?) ON CONFLICT(snapshot_id) DO UPDATE SET "
        "data_version=excluded.data_version, document=excluded.document, "
        "computed_at=excluded.computed_at",
        (snapshot_id, data_version, Database.dumps(document), computed_at),
    )


def load_analysis(snapshot_id: str, data_version: str) -> Optional[Dict[str, Any]]:
    """
    The stored analysis for this snapshot, only if it was computed from THIS
    version of the network. A re-upload changes `data_version`, so a stale
    analysis is never served against fresh data — it is recomputed.
    """
    row = database.query_one(
        "SELECT document, computed_at FROM analyses "
        "WHERE snapshot_id = ? AND data_version = ?",
        (snapshot_id, data_version),
    )
    if row is None:
        return None
    document = Database.loads(row["document"])
    if not document:
        return None
    document["computed_at"] = row["computed_at"]
    return document


def delete_analysis(snapshot_id: str) -> None:
    database.execute("DELETE FROM analyses WHERE snapshot_id = ?", (snapshot_id,))


def load_all_analyses() -> List[Tuple[str, str, Dict[str, Any], float]]:
    return [(r["snapshot_id"], r["data_version"], Database.loads(r["document"]),
             r["computed_at"])
            for r in database.query(
                "SELECT snapshot_id, data_version, document, computed_at FROM analyses")]


# -- Login throttling ------------------------------------------------------
#
# In the DATABASE, not in process memory. A lockout that resets when the server
# restarts is not a lockout, and one that only the web worker handling this
# request knows about is not one either the moment a second worker exists.

def record_login_failure(identity: str, now: float, window: float,
                         threshold: int, lock_seconds: float) -> Dict[str, Any]:
    """
    Count one failed attempt and return the resulting state.

    The failure window is rolling: a burst spread thinly enough to fall outside
    it starts the count again, so an ordinary user who mistypes a password
    twice a week is never locked out.
    """
    row = database.query_one(
        "SELECT failures, first_failed, locked_until FROM login_attempts "
        "WHERE identity = ?", (identity,))
    if row is None or (now - float(row["first_failed"])) > window:
        failures, first_failed = 1, now
    else:
        failures, first_failed = int(row["failures"]) + 1, float(row["first_failed"])

    locked_until = now + lock_seconds if failures >= threshold else None
    database.execute(
        "INSERT INTO login_attempts(identity, failures, first_failed, last_failed, "
        "locked_until) VALUES(?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
        "failures=excluded.failures, first_failed=excluded.first_failed, "
        "last_failed=excluded.last_failed, locked_until=excluded.locked_until",
        (identity, failures, first_failed, now, locked_until),
    )
    return {"failures": failures, "locked_until": locked_until}


def login_lock_state(identity: str) -> Optional[Dict[str, Any]]:
    row = database.query_one(
        "SELECT failures, locked_until FROM login_attempts WHERE identity = ?",
        (identity,))
    if row is None:
        return None
    return {"failures": int(row["failures"]),
            "locked_until": row["locked_until"]}


def clear_login_failures(identity: str) -> None:
    database.execute("DELETE FROM login_attempts WHERE identity = ?", (identity,))


# -- Password reset --------------------------------------------------------

def save_password_reset(token_hash: str, user_id: str, created_at: float,
                        expires_at: float) -> None:
    database.execute(
        "INSERT INTO password_resets(token_hash, user_id, created_at, expires_at) "
        "VALUES(?,?,?,?) ON CONFLICT(token_hash) DO NOTHING",
        (token_hash, user_id, created_at, expires_at),
    )


def load_password_reset(token_hash: str) -> Optional[Dict[str, Any]]:
    return database.query_one(
        "SELECT token_hash, user_id, created_at, expires_at, used_at "
        "FROM password_resets WHERE token_hash = ?", (token_hash,))


def consume_password_reset(token_hash: str, used_at: float) -> int:
    """
    Mark a reset token used, and report whether THIS call was the one that did.

    The `used_at IS NULL` predicate is what makes the token single-use under
    concurrency: two simultaneous redemptions both read an unused row, and
    exactly one of them updates it.
    """
    return database.execute(
        "UPDATE password_resets SET used_at = ? "
        "WHERE token_hash = ? AND used_at IS NULL",
        (used_at, token_hash),
    )


def invalidate_password_resets(user_id: str, used_at: float) -> int:
    """Burn every outstanding reset for a user — after one succeeds."""
    return database.execute(
        "UPDATE password_resets SET used_at = ? "
        "WHERE user_id = ? AND used_at IS NULL", (used_at, user_id))


def count_recent_password_resets(user_id: str, since: float) -> int:
    row = database.query_one(
        "SELECT COUNT(*) AS n FROM password_resets "
        "WHERE user_id = ? AND created_at >= ?", (user_id, since))
    return int(row["n"]) if row else 0


# -- Sessions, with the fields that make them auditable --------------------

def save_session_record(token: str, user_id: str, expires_at: float,
                        created_at: float, last_seen_at: float,
                        absolute_expiry: float, client: str) -> None:
    database.execute(
        "INSERT INTO sessions(token, user_id, expires_at, created_at, "
        "last_seen_at, absolute_expiry, client) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET expires_at=excluded.expires_at, "
        "last_seen_at=excluded.last_seen_at",
        (token, user_id, expires_at, created_at, last_seen_at,
         absolute_expiry, client),
    )


def touch_session(token: str, expires_at: float, last_seen_at: float) -> None:
    database.execute(
        "UPDATE sessions SET expires_at = ?, last_seen_at = ? WHERE token = ?",
        (expires_at, last_seen_at, token))


def load_session_records(now: float) -> List[Dict[str, Any]]:
    return database.query(
        "SELECT token, user_id, expires_at, created_at, last_seen_at, "
        "absolute_expiry, client FROM sessions WHERE expires_at >= ?", (now,))


def delete_sessions_for_user(user_id: str, keep_token: str = "") -> int:
    """Revoke every session for a user — 'sign out everywhere'."""
    if keep_token:
        return database.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token <> ?",
            (user_id, keep_token))
    return database.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# -- MFA -------------------------------------------------------------------

def save_mfa_enrolment(user_id: str, secret: str, created_at: float,
                       confirmed_at: Optional[float]) -> None:
    database.execute(
        "INSERT INTO mfa_enrolments(user_id, secret, created_at, confirmed_at) "
        "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
        "secret=excluded.secret, created_at=excluded.created_at, "
        "confirmed_at=excluded.confirmed_at",
        (user_id, secret, created_at, confirmed_at),
    )


def load_mfa_enrolment(user_id: str) -> Optional[Dict[str, Any]]:
    return database.query_one(
        "SELECT user_id, secret, created_at, confirmed_at, last_used_step "
        "FROM mfa_enrolments WHERE user_id = ?", (user_id,))


def confirm_mfa_enrolment(user_id: str, confirmed_at: float) -> None:
    database.execute(
        "UPDATE mfa_enrolments SET confirmed_at = ? WHERE user_id = ?",
        (confirmed_at, user_id))


def claim_mfa_step(user_id: str, step: int) -> bool:
    """
    Record the time-step a code was accepted for, refusing a repeat.

    Without this a captured six-digit code can be replayed for the rest of its
    30-second window — the one attack a TOTP implementation is most likely to
    leave open. The `last_used_step IS NULL OR last_used_step < ?` predicate
    makes the claim atomic, so two simultaneous submissions of the same code
    cannot both win.
    """
    changed = database.execute(
        "UPDATE mfa_enrolments SET last_used_step = ? WHERE user_id = ? "
        "AND (last_used_step IS NULL OR last_used_step < ?)",
        (step, user_id, step))
    return changed > 0


def delete_mfa_enrolment(user_id: str) -> None:
    database.execute("DELETE FROM mfa_enrolments WHERE user_id = ?", (user_id,))
    database.execute("DELETE FROM mfa_recovery_codes WHERE user_id = ?", (user_id,))


def save_recovery_codes(user_id: str, code_hashes: Iterable[str]) -> None:
    database.execute("DELETE FROM mfa_recovery_codes WHERE user_id = ?", (user_id,))
    for code_hash in code_hashes:
        database.execute(
            "INSERT INTO mfa_recovery_codes(user_id, code_hash, used) "
            "VALUES(?,?,0) ON CONFLICT(user_id, code_hash) DO NOTHING",
            (user_id, code_hash))


def consume_recovery_code(user_id: str, code_hash: str) -> bool:
    """Spend one recovery code. Single-use, enforced by the `used = 0` predicate."""
    return database.execute(
        "UPDATE mfa_recovery_codes SET used = 1 "
        "WHERE user_id = ? AND code_hash = ? AND used = 0",
        (user_id, code_hash)) > 0


def count_unused_recovery_codes(user_id: str) -> int:
    row = database.query_one(
        "SELECT COUNT(*) AS n FROM mfa_recovery_codes WHERE user_id = ? AND used = 0",
        (user_id,))
    return int(row["n"]) if row else 0


# -- Shared rate-limit windows ---------------------------------------------

def bump_rate_limit_window(bucket: str, client: str, now: float,
                           window_seconds: float) -> Tuple[int, float]:
    """
    Count one request against a window every worker shares, atomically.

    Returns `(hits_in_window, window_start)`.

    The whole read-decide-write is ONE statement, because the interesting case
    is two workers arriving at the same instant. Reading the row, deciding, and
    writing it back would let both read the same count and both write the same
    increment, which is precisely the burst the limit exists to catch.

    The window is fixed rather than sliding, for the same reason it always was:
    a sliding window needs the timestamp of every request in it, which is
    unbounded storage for an endpoint under attack.
    """
    rows = database.query(
        "INSERT INTO rate_limit_windows(bucket, client, window_start, hits) "
        "VALUES(?,?,?,1) "
        "ON CONFLICT(bucket, client) DO UPDATE SET "
        "  hits = CASE WHEN ? - rate_limit_windows.window_start >= ? "
        "              THEN 1 ELSE rate_limit_windows.hits + 1 END, "
        "  window_start = CASE WHEN ? - rate_limit_windows.window_start >= ? "
        "              THEN ? ELSE rate_limit_windows.window_start END "
        "RETURNING hits, window_start",
        (bucket, client, now, now, window_seconds, now, window_seconds, now),
    )
    if not rows:
        # No RETURNING row should be impossible, but a limiter that cannot read
        # its own counter must refuse rather than wave the request through.
        return 1, now
    return int(rows[0]["hits"]), float(rows[0]["window_start"])


def purge_rate_limit_windows(older_than: float) -> int:
    """Drop windows nobody is in any more."""
    return database.execute(
        "DELETE FROM rate_limit_windows WHERE window_start < ?", (older_than,))


def clear_rate_limit_windows() -> None:
    """For tests, so one test's attempts do not exhaust another's budget."""
    database.execute("DELETE FROM rate_limit_windows")


# -- Execution traces ------------------------------------------------------

def save_execution_trace(execution_id: str, document: Dict[str, Any], *,
                         actor_id: str = "", intent: str = "",
                         workflow: str = "", snapshot_id: str = "",
                         status: str = "", started_at: float = 0.0) -> None:
    database.execute(
        "INSERT INTO execution_traces(execution_id, actor_id, intent, workflow, "
        "snapshot_id, status, started_at, document) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(execution_id) DO UPDATE SET status=excluded.status, "
        "document=excluded.document",
        (execution_id, actor_id, intent, workflow, snapshot_id, status,
         started_at, database.dumps(document)),
    )


def load_execution_trace(execution_id: str) -> Optional[Dict[str, Any]]:
    row = database.query_one(
        "SELECT document FROM execution_traces WHERE execution_id = ?",
        (execution_id,))
    return database.loads(row["document"]) if row else None


def load_execution_traces(limit: int = 200,
                          actor_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Most recent first. `actor_id` narrows to one user's executions."""
    if actor_id:
        rows = database.query(
            "SELECT document FROM execution_traces WHERE actor_id = ? "
            "ORDER BY started_at DESC LIMIT ?", (actor_id, limit))
    else:
        rows = database.query(
            "SELECT document FROM execution_traces "
            "ORDER BY started_at DESC LIMIT ?", (limit,))
    return [database.loads(r["document"]) for r in rows]


def purge_execution_traces(older_than: float) -> int:
    return database.execute(
        "DELETE FROM execution_traces WHERE started_at < ?", (older_than,))


def count_execution_traces() -> int:
    return database.count("execution_traces")


# -- Federated identity ----------------------------------------------------

def link_federated_identity(issuer: str, subject: str, user_id: str,
                            email: str, now: float) -> None:
    database.execute(
        "INSERT INTO federated_identities(issuer, subject, user_id, email, "
        "created_at, last_seen) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(issuer, subject) DO UPDATE SET last_seen=excluded.last_seen, "
        "email=excluded.email",
        (issuer, subject, user_id, email, now, now),
    )


def find_federated_identity(issuer: str, subject: str) -> Optional[Dict[str, Any]]:
    return database.query_one(
        "SELECT issuer, subject, user_id, email, created_at, last_seen "
        "FROM federated_identities WHERE issuer = ? AND subject = ?",
        (issuer, subject))


def federated_identities_for(user_id: str) -> List[Dict[str, Any]]:
    return database.query(
        "SELECT issuer, subject, email, created_at, last_seen "
        "FROM federated_identities WHERE user_id = ? ORDER BY created_at",
        (user_id,))


def unlink_federated_identity(issuer: str, subject: str) -> int:
    return database.execute(
        "DELETE FROM federated_identities WHERE issuer = ? AND subject = ?",
        (issuer, subject))


def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Run a persistence write without letting it break the request.

    A failed WRITE must not take down a solve that has already succeeded — the
    in-memory state is still correct for this process, and the user gets their
    answer. It is logged at error level so the failure is visible rather than
    silent. A failed READ at start-up is a different matter and is not guarded:
    starting with a partially-loaded database would present a user with some of
    their projects and hide the rest.
    """
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("persistence.write_failed fn=%s error=%s", fn.__name__, exc)
            return None
    return wrapper
