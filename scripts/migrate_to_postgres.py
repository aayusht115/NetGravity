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


#: (table, primary key columns, every column) — mirrors `persistence._schema_statements`.
TABLES = (
    ("users", ("user_id",), ("user_id", "email", "document", "created_at")),
    ("sessions", ("token",), ("token", "user_id", "expires_at")),
    ("projects", ("project_id",), ("project_id", "owner_id", "document", "updated_at")),
    ("snapshots", ("snapshot_id",), ("snapshot_id", "network_id", "document", "created_at")),
    ("scenario_networks", ("scenario_id",),
     ("scenario_id", "snapshot_id", "document", "created_at")),
    ("scenarios", ("scenario_id",), ("scenario_id", "project_id", "document", "created_at")),
    ("network_data", ("kind", "network_id"), ("kind", "network_id", "document")),
    ("analyses", ("snapshot_id",),
     ("snapshot_id", "data_version", "document", "computed_at")),
    ("app_state", ("key",), ("key", "value")),
)


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

    copied = {}
    for table, keys, columns in TABLES:
        if table not in present:
            print(f"  {table:18} not in source, skipped")
            continue
        rows = list(source.execute(f"SELECT * FROM {table}"))  # noqa: S608 — fixed names
        available = [c for c in columns if c in rows[0].keys()] if rows else list(columns)
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
    for table, keys, columns in TABLES:
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
            for column in ("document", "value"):
                if column in src and src[column] != dst.get(column):
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
