"""
Moving a store to PostgreSQL without leaving anything behind.

THE FAILURE THIS GUARDS AGAINST IS SILENT, which is what makes it worth a test
file of its own. `scripts/migrate_to_postgres.py` carried a hand-written list
of nine tables against the sixteen the schema creates. It copied those nine,
verified those nine, and printed "Migrated N rows" — a success report from a
migration that had left behind every MFA enrolment, every recovery code, every
outstanding password reset, every login lockout, every execution trace and
every federated identity link.

A user with a second factor arrived on the new database with it gone. A user
signing in through SSO arrived with no link between their provider identity and
their account. Nothing raised, and no row count disagreed, because the counts
were only ever taken over the tables the list already knew about.

So the list is no longer written by hand anywhere. The schema owns it, and
these tests hold the schema and the list together.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import tempfile

import pytest

from app.backend.services.migrations import (
    APPLICATION_TABLES,
    MIGRATION_BOOKKEEPING_TABLE,
    MIGRATIONS,
    TABLE_NAMES,
    apply_migrations,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS_PY = REPO_ROOT / "app" / "backend" / "services" / "migrations.py"
SCRIPT = REPO_ROOT / "scripts" / "migrate_to_postgres.py"


def _tables_the_schema_creates() -> set:
    """Every table name any migration issues a CREATE TABLE for."""
    found = set()
    for migration in MIGRATIONS:
        for dialect in ("sqlite", "postgres"):
            for statement in migration.statements(dialect):
                match = re.search(
                    r"CREATE TABLE IF NOT EXISTS\s+(\w+)", statement, re.I)
                if match:
                    found.add(match.group(1))
    return found


class TestTheListCannotDriftFromTheSchema:

    def test_every_table_the_schema_creates_is_in_the_list(self):
        """
        The one that failed. Migrations 2, 3, 5 and 6 each added a table and
        neither of the two hand-maintained lists was touched.
        """
        created = _tables_the_schema_creates()
        listed = set(TABLE_NAMES)
        missing = created - listed - {MIGRATION_BOOKKEEPING_TABLE}
        assert not missing, (
            f"the schema creates {sorted(missing)} and nothing copies them")

    def test_the_list_names_no_table_the_schema_does_not_create(self):
        created = _tables_the_schema_creates()
        invented = set(TABLE_NAMES) - created
        assert not invented, sorted(invented)

    def test_the_runner_s_own_bookkeeping_is_never_copied(self):
        """
        `schema_migrations` is written by `apply_migrations` on the TARGET as
        it builds the schema. Copying the source's rows over it would tell a
        fresh database that migrations had run which had not — and the next
        deployment would skip them.
        """
        assert MIGRATION_BOOKKEEPING_TABLE not in TABLE_NAMES

    def test_persistence_reads_the_same_list(self):
        from app.backend.services.persistence import TABLES
        assert TABLES == TABLE_NAMES

    def test_the_migration_script_reads_it_too(self):
        """
        It must not carry a list of its own — that is what went stale. Checked
        against the CODE, not the comment block that explains what it replaced
        and therefore names the old symbol.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        code = re.sub(r'"""[\s\S]*?"""', "", source)
        code = re.sub(r"^\s*#.*$", "", code, flags=re.M)
        assert "APPLICATION_TABLES" in code
        assert not re.search(r"^TABLES\s*=\s*\(", code, re.M), \
            "the script has grown its own table list again"


class TestEveryTableCarriesAKeyThatIdentifiesARow:
    """
    The copy is an upsert `ON CONFLICT(<keys>)`. A key that is not the table's
    real primary key makes the migration either fail outright or — worse —
    silently overwrite one row with another.
    """

    def _primary_keys(self, table: str) -> set:
        for migration in MIGRATIONS:
            for statement in migration.statements("sqlite"):
                if not re.search(rf"CREATE TABLE IF NOT EXISTS\s+{table}\b",
                                 statement, re.I):
                    continue
                composite = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", statement, re.I)
                if composite:
                    return {c.strip() for c in composite.group(1).split(",")}
                # The character class must not cross a line. A negated
                # class matches newlines too, so the loose version ran
                # from "CREATE TABLE IF NOT EXISTS users (" across the
                # break to the "PRIMARY KEY" on the next line, and
                # reported the primary key of every table as "CREATE".
                inline = re.findall(
                    r"^\s*(\w+)\s+\w+[^,\n]*PRIMARY KEY",
                    statement, re.M | re.I)
                if inline:
                    return set(inline)
        return set()

    @pytest.mark.parametrize("table,keys", APPLICATION_TABLES)
    def test_the_declared_key_is_the_tables_real_primary_key(self, table, keys):
        actual = self._primary_keys(table)
        assert actual, f"{table}: no PRIMARY KEY found in the schema"
        assert set(keys) == actual, f"{table}: declared {keys}, schema says {actual}"


class TestTheSchemaBuildsOnASqliteStore:
    """
    Not a PostgreSQL test — this machine has no server. What it does establish
    is that the migration list and the schema agree on a real database: every
    table the list names is genuinely creatable and genuinely present after
    the migrations run.
    """

    def test_a_fresh_store_has_every_table_the_list_names(self):
        from app.backend.services.persistence import Database

        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "fresh.db")
            db = Database(path=path)
            try:
                assert db.kind == "sqlite"
                present = {r["name"] for r in db.query(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                db.close()
            for table in TABLE_NAMES:
                assert table in present, table

    def test_every_table_can_be_counted(self):
        """
        `/api/status` counts each of these. A name in the list that is not a
        real table would take the status endpoint down.
        """
        from app.backend.services.persistence import Database

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(path=str(pathlib.Path(tmp) / "counted.db"))
            try:
                for table in TABLE_NAMES:
                    assert db.count(table) == 0
            finally:
                db.close()


class TestTheMigrationRefusesToLoseRows:

    def test_an_unknown_source_table_stops_the_migration(self):
        """
        A table in the source that the schema list does not know is either a
        migration nobody registered or a build this script does not
        understand. Both mean rows would be left behind — so it is refused,
        not skipped with a line in a log that scrolls past.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        code = re.sub(r'"""[\s\S]*?"""', "", source)
        assert "raise SystemExit(" in code
        assert "does not know" in code

    def test_the_verification_compares_every_column(self):
        """
        It compared `document` and `value` only. On `mfa_enrolments` the
        column that matters is `secret`, and it was never looked at — so a
        secret that failed to copy verified clean.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        block = source[source.index("verifying..."):]
        assert 'for column, value in src.items():' in block
        assert 'for column in ("document", "value")' not in block

    def test_it_still_opens_the_source_read_only(self):
        source = SCRIPT.read_text(encoding="utf-8")
        assert "mode=ro" in source and "uri=True" in source


class TestTheSqliteFallbackIsVisibleNotSilent:
    """
    The one thing worse than refusing to start is starting on the wrong store:
    the process runs, looks healthy, and writes a client's work to a local file
    nobody is backing up.

    THE MOVE ITSELF IS DEFERRED, so `require_supported_store()` is not wired
    into startup — calling it today would only stop the application, there
    being no PostgreSQL to move to. What is held here is that the pieces are
    correct for the day it is: a CONFIGURED PostgreSQL that cannot be reached
    must never silently degrade to a file, and the store in use is reported.
    """

    def test_a_configured_postgres_that_cannot_be_reached_is_fatal(self):
        source = (REPO_ROOT / "app" / "backend" / "services"
                  / "persistence.py").read_text(encoding="utf-8")
        # THE BRANCH THAT HAS A URL, and nothing past it. A wider slice sweeps
        # in the `else:` that opens SQLite when no URL was configured at all,
        # and reports a fallback that is not there.
        marker = "        if url:" + chr(10)
        start = source.index(marker)
        block = source[start:source.index(chr(10) + "        else:", start)]
        assert "raise RuntimeError(" in block
        assert "_SQLiteBackend" not in block, \
            "a failed PostgreSQL connection must not fall back to a file"
        # The message names the store it could not reach with the password
        # redacted — an operator needs to know WHICH database, and must not be
        # handed a credential in a log.
        assert "_redact_url(url)" in block

    def test_the_gate_exists_even_though_it_is_not_wired_in(self):
        """
        Kept, uncalled, for the day the move happens. Deleting it would mean
        writing it again from the same reasoning.
        """
        from app.backend.services.persistence import require_supported_store
        assert callable(require_supported_store)

    def test_the_application_still_starts_on_sqlite_today(self):
        """
        The gate was briefly wired into startup and had to come back out: with
        the move deferred, the only thing it could do was stop the application
        from starting at all.
        """
        source = (REPO_ROOT / "app" / "backend"
                  / "app.py").read_text(encoding="utf-8")
        code = re.sub(r"^\s*#.*$", "", source, flags=re.M)
        assert "require_supported_store(" not in code

    def test_the_store_in_use_is_reported(self):
        source = (REPO_ROOT / "app" / "backend"
                  / "app.py").read_text(encoding="utf-8")
        assert '_STORAGE["supported"]' in source
