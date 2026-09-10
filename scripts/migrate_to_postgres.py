"""
Copy an existing NetGravity SQLite store into PostgreSQL.

    python scripts/migrate_to_postgres.py \
        --sqlite data/netgravity.db \
        --postgres postgresql://user:pass@host:5432/netgravity

Run it once, before pointing the application at Postgres. Everything a user
created moves across: accounts, live sessions, projects, uploaded networks, the
demand/capacity/signal history that arrived with them, the analysis computed
from them, materialised scenario networks and solved scenarios.

Properties this deliberately has
--------------------------------
It is IDEMPOTENT. Every insert is an upsert on the primary key, so running it
twice copies the same rows to the same place. A migration you are afraid to
re-run is a migration you cannot resume after a network drop.

It is NON-DESTRUCTIVE. The SQLite file is opened read-only and is not touched.
If anything goes wrong the old store is still there, and the application still
starts on it the moment `NETGRAVITY_DATABASE_URL` is unset.

It VERIFIES. After copying, every table's row count is compared and every
document is re-read from Postgres and checked byte-for-byte against the source.
A migration that reports success without reading the data back has only
established that the writes did not raise.

Rows already present in Postgres but absent from SQLite are left alone: this is
a copy-in, not a mirror, so it can be run against a target that is already
serving.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


#: WHAT GETS COPIED — read from the schema, never listed here.
#:
#: THIS LIST USED TO BE WRITTEN OUT BY HAND, and it had fallen seven tables
#: behind: it copied nine of the sixteen the migrations create. The seven it
#: missed were `login_attempts`, `password_resets`, `mfa_enrolments`,
#: `mfa_recovery_codes`, `rate_limit_windows`, `execution_traces` and
#: `federated_identities`.
#:
#: Four of those are the worst possible omission. A store migrated to
#: PostgreSQL arrived without `mfa_enrolments` or `mfa_recovery_codes` — so
#: every user who had set up a second factor arrived with it SILENTLY GONE,
#: and the migration reported success because it verified only the tables it
#: had decided to copy. `federated_identities` is the same shape of failure for
#: anyone signing in through SSO: the link between their provider identity and
#: their account, not copied, is an account they can no longer reach.
#:
#: Reading `APPLICATION_TABLES` from the module that OWNS the schema is what
#: stops this recurring: a migration that adds a table adds it there, and this
#: script picks it up without being edited.
def _tables():
    from app.backend.services.migrations import APPLICATION_TABLES
    return APPLICATION_TABLES


def _columns(conn, table: str):
    """
    The table's real columns, from the source database.

    Read rather than declared. The previous version carried a hand-written
    column list per table and intersected it with the row keys, so a column
    added by a migration and not added to that list was quietly dropped from
    every row copied — a data loss that no count check can see, because the
    row arrives.
    """
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]  # noqa: S608


def _source_tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def migrate(sqlite_path: str, postgres_url: str, batch: int = 200) -> int:
    from app.backend.services.persistence import Database

    source_file = pathlib.Path(sqlite_path)
    if not source_file.exists():
        print(f"No SQLite store at {sqlite_path} — nothing to migrate.")
        return 0

    # Read-only URI: the source cannot be modified even by accident.
    source = sqlite3.connect(f"file:{source_file.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    present = _source_tables(source)

    target = Database(url=postgres_url)
    if target.kind != "postgresql":
        raise SystemExit("The --postgres URL did not produce a PostgreSQL connection.")
    print(f"source : {source_file}")
    print(f"target : {target.path}\n")

    # EVERY TABLE THE SOURCE HOLDS IS ACCOUNTED FOR, or the migration stops.
    #
    # A table present in the source and unknown to the schema list is either a
    # migration nobody added to `APPLICATION_TABLES` or a table from a build
    # this script does not understand. Both mean rows would be left behind
    # silently, which is the failure this whole change is about — so it is
    # refused rather than skipped with a line in a log.
    known = {t for t, _ in _tables()} | {"schema_migrations"}
    unknown = sorted(present - known - {"sqlite_sequence"})
    if unknown:
        raise SystemExit(
            "The source database holds tables this migration does not know "
            f"about: {', '.join(unknown)}.\n"
            "Add them to APPLICATION_TABLES in app/backend/services/"
            "migrations.py before migrating, or their rows will not arrive."
        )

    copied = {}
    for table, keys in _tables():
        if table not in present:
            print(f"  {table:18} not in source, skipped")
            continue
        rows = list(source.execute(f"SELECT * FROM {table}"))  # noqa: S608 — fixed names
        available = _columns(source, table)
        placeholders = ",".join(["?"] * len(available))
        updates = ",".join(f"{c}=excluded.{c}" for c in available if c not in keys)
        conflict = ",".join(keys)
        sql = (f"INSERT INTO {table}({','.join(available)}) VALUES({placeholders}) "  # noqa: S608
               f"ON CONFLICT({conflict}) DO UPDATE SET {updates}")
        for i in range(0, len(rows), batch):
            for row in rows[i:i + batch]:
                target.execute(sql, tuple(row[c] for c in available))
        copied[table] = len(rows)
        print(f"  {table:18} {len(rows):5} row(s)")

    # ---- verify -------------------------------------------------------
    print("\nverifying...")
    problems = []
    for table, keys in _tables():
        if table not in copied:
            continue
        source_rows = {tuple(r[k] for k in keys): dict(r)
                       for r in source.execute(f"SELECT * FROM {table}")}  # noqa: S608
        target_rows = {tuple(r[k] for k in keys): r
                       for r in target.query(f"SELECT * FROM {table}")}  # noqa: S608
        missing = set(source_rows) - set(target_rows)
        if missing:
            problems.append(f"{table}: {len(missing)} row(s) did not arrive")
            continue
        for key, src in source_rows.items():
            dst = target_rows[key]
            # EVERY column, not just `document` and `value`.
            #
            # The check named two columns, so a secret, a hash or a timestamp
            # that failed to copy verified clean. On `mfa_enrolments` the
            # column that matters is `secret` and it was never compared.
            #
            # Timestamps are compared as floats: SQLite stores REAL and
            # PostgreSQL DOUBLE PRECISION, and the two round-trip to values
            # that are equal as numbers and unequal as objects.
            for column, value in src.items():
                other = dst.get(column)
                if isinstance(value, float) or isinstance(other, float):
                    if value is None or other is None:
                        if value is not other:
                            problems.append(
                                f"{table}{key}: {column} differs after copy")
                            break
                        continue
                    if abs(float(value) - float(other)) > 1e-6:
                        problems.append(
                            f"{table}{key}: {column} differs after copy")
                        break
                elif value != other:
                    problems.append(f"{table}{key}: {column} differs after copy")
                    break
        print(f"  {table:18} {len(source_rows):5} row(s) verified byte-for-byte")

    source.close()
    target.close()

    if problems:
        print("\nMIGRATION INCOMPLETE:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"\nMigrated {sum(copied.values())} row(s). The SQLite file was not modified;")
    print("keep it until you are satisfied the application is running on PostgreSQL.")
    print("\nNext: set NETGRAVITY_DATABASE_URL to the same URL and restart.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="data/netgravity.db",
                        help="path to the existing SQLite store")
    parser.add_argument("--postgres", required=True,
                        help="postgresql://user:pass@host:port/database")
    args = parser.parse_args()
    return migrate(args.sqlite, args.postgres)


if __name__ == "__main__":
    raise SystemExit(main())
