"""
Take a verified backup of the NetGravity store.

    python scripts/backup_database.py --out backups/
    python scripts/backup_database.py --out backups/ --verify-restore

Why this exists rather than a line in a runbook
-----------------------------------------------
"Set up backups" is the instruction that gets deferred until the day it is
needed. This produces one, from the database the application is actually
configured against, and — with `--verify-restore` — proves the dump can be
restored by restoring it into a scratch database and comparing row counts.

An unverified backup is a belief, not a backup. The only way to know a dump
restores is to restore it.

PostgreSQL
----------
Uses `pg_dump` in custom format (`-Fc`): compressed, and restorable
selectively with `pg_restore`. `pg_dump` must be on PATH, or named by
`NETGRAVITY_PG_DUMP`.

SQLite
------
Uses the online backup API through `sqlite3`, which is safe against a live
database — unlike copying the file, which can capture a torn write mid-WAL.

This is the LOGICAL backup, and it is not the whole story
--------------------------------------------------------
A `pg_dump` is a snapshot of the data as of one instant. It restores that
instant and nothing else: you cannot replay WAL onto it, so the recovery point
is however old the last dump is — up to 24 hours on a nightly schedule.

Point-in-time recovery needs a PHYSICAL base backup plus archived WAL, and both
are `scripts/pitr.py`:

    python scripts/pitr.py status                     # is archiving on?
    python scripts/pitr.py configure --data-dir <PGDATA>
    python scripts/pitr.py basebackup --out <DIR>      # the physical backup
    python scripts/pitr.py drill                       # prove a recovery works

Run both. They protect against different things: this dump survives a corrupted
cluster and moves between machines and major versions; PITR recovers to the
minute before a mistake. Neither substitutes for the other.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Every table whose row count is compared before and after a verify-restore.
from app.backend.services.persistence import TABLES  # noqa: E402


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _counts(db) -> dict:
    out = {}
    for table in TABLES:
        try:
            out[table] = db.count(table)
        except Exception:  # noqa: BLE001 — a table absent from an old dump is reported as such
            out[table] = None
    return out


def backup_sqlite(path: str, out_dir: pathlib.Path) -> pathlib.Path:
    import sqlite3

    target = out_dir / f"netgravity-{_timestamp()}.sqlite"
    source = sqlite3.connect(f"file:{pathlib.Path(path).as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(str(target))
    with destination:
        # The online backup API, not a file copy: it is consistent against a
        # database being written to, which a copy is not.
        source.backup(destination)
    source.close()
    destination.close()
    return target


def backup_postgres(url: str, out_dir: pathlib.Path) -> pathlib.Path:
    pg_dump = os.environ.get("NETGRAVITY_PG_DUMP") or shutil.which("pg_dump")
    if not pg_dump:
        raise SystemExit(
            "pg_dump was not found on PATH. Install the PostgreSQL client tools, "
            "or set NETGRAVITY_PG_DUMP to its location."
        )
    target = out_dir / f"netgravity-{_timestamp()}.dump"
    result = subprocess.run(
        [pg_dump, "--no-password", "--format=custom", "--no-owner",
         "--no-privileges", "--file", str(target), url],
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        # stderr can echo the connection string; the password is in it.
        raise SystemExit(f"pg_dump failed with exit {result.returncode}. "
                         f"Check the connection and the target directory.")
    return target


def _sibling_tool(name: str) -> str:
    """
    Find one PostgreSQL client tool.

    Checks its own environment variable, then PATH, then **the directory
    whatever other tool was named explicitly lives in** — because the tools ship
    together. Being told where `pg_dump` is and then reporting `pg_restore` as
    missing, when it is in the same folder, turned a working verification into a
    refusal to verify.
    """
    specific = os.environ.get(f"NETGRAVITY_{name.upper()}")
    if specific and pathlib.Path(specific).exists():
        return specific
    found = shutil.which(name)
    if found:
        return found
    for hint in ("NETGRAVITY_PG_DUMP", "NETGRAVITY_PG_BINDIR", "NETGRAVITY_PSQL"):
        value = os.environ.get(hint)
        if not value:
            continue
        base = pathlib.Path(value)
        bindir = base if base.is_dir() else base.parent
        for candidate in (bindir / name, bindir / f"{name}.exe"):
            if candidate.exists():
                return str(candidate)
    return ""


def verify_restore_postgres(dump: pathlib.Path, url: str, expected: dict) -> bool:
    """Restore into a scratch database and compare every table's row count."""
    pg_restore = _sibling_tool("pg_restore")
    psql = _sibling_tool("psql")
    if not pg_restore or not psql:
        print("  pg_restore/psql not found — restore NOT verified.")
        return False

    base, _, database = url.rpartition("/")
    scratch = f"{database.split('?')[0]}_restorecheck_{int(time.time())}"
    admin_url = f"{base}/postgres"
    try:
        subprocess.run([psql, "-w", "-d", admin_url, "-c",
                        f'CREATE DATABASE "{scratch}"'],
                       capture_output=True, text=True, check=True, timeout=60)
        exists = subprocess.run(
            [psql, "-w", "-t", "-d", admin_url, "-c",
             f"SELECT 1 FROM pg_database WHERE datname = '{scratch}'"],
            capture_output=True, text=True, timeout=60)
        if "1" not in (exists.stdout or ""):
            print("  the scratch database was not created — restore NOT verified.")
            return False

        restore = subprocess.run(
            [pg_restore, "--no-password", "--no-owner", "--no-privileges",
             "--dbname", f"{base}/{scratch}", str(dump)],
            capture_output=True, text=True, timeout=600)
        if restore.returncode != 0:
            print(f"  pg_restore exited {restore.returncode} — restore FAILED.")
            for line in (restore.stderr or "").splitlines()[:6]:
                print(f"    {line}")
            return False

        from app.backend.services.persistence import Database
        restored = Database(url=f"{base}/{scratch}")
        actual = _counts(restored)
        restored.close()

        mismatched = {t: (expected[t], actual.get(t))
                      for t in TABLES if expected.get(t) != actual.get(t)}
        for table in TABLES:
            print(f"    {table:22} {expected.get(table)} -> {actual.get(table)}")
        if mismatched:
            print(f"  MISMATCH after restore: {mismatched}")
            return False
        return True
    finally:
        subprocess.run([psql, "-w", "-d", admin_url, "-c",
                        f'DROP DATABASE IF EXISTS "{scratch}"'],
                       capture_output=True, text=True, timeout=60)


def verify_restore_sqlite(dump: pathlib.Path, expected: dict) -> bool:
    from app.backend.services.persistence import Database

    restored = Database(path=str(dump))
    actual = _counts(restored)
    restored.close()
    mismatched = {t: (expected[t], actual.get(t))
                  for t in TABLES if expected.get(t) != actual.get(t)}
    for table in TABLES:
        print(f"    {table:22} {expected.get(table)} -> {actual.get(table)}")
    if mismatched:
        print(f"  MISMATCH after restore: {mismatched}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="backups", help="directory to write into")
    parser.add_argument("--verify-restore", action="store_true",
                        help="restore the dump into a scratch database and "
                             "compare row counts")
    parser.add_argument("--keep", type=int, default=14,
                        help="how many backups to retain (0 keeps all)")
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    from app.backend.services.persistence import configured_database_url, database

    expected = _counts(database)
    total = sum(v for v in expected.values() if isinstance(v, int))
    print(f"source : {database.kind} at {database.path}")
    print(f"rows   : {total} across {len(TABLES)} tables")

    url = configured_database_url()
    if database.kind == "postgresql" and url:
        dump = backup_postgres(url, out_dir)
    else:
        dump = backup_sqlite(database.path, out_dir)
    size = dump.stat().st_size
    print(f"wrote  : {dump} ({size:,} bytes)")

    if size == 0:
        print("The dump is EMPTY. Treat this as a failed backup.")
        return 1

    if args.verify_restore:
        print("verifying by restoring...")
        ok = (verify_restore_postgres(dump, url, expected)
              if database.kind == "postgresql"
              else verify_restore_sqlite(dump, expected))
        if not ok:
            print("BACKUP NOT VERIFIED — do not rely on it.")
            return 1
        print("verified: every table restored with the same row count.")

    if args.keep > 0:
        pattern = "netgravity-*.dump" if database.kind == "postgresql" else "netgravity-*.sqlite"
        existing = sorted(out_dir.glob(pattern))
        for stale in existing[:-args.keep]:
            stale.unlink()
            print(f"pruned : {stale.name}")

    # A verified dump is not point-in-time recovery, and the difference is a
    # whole day of data. Said here, at the moment someone has just been told
    # their backup is verified, rather than only in a document they may not
    # read.
    if database.kind == "postgresql":
        print()
        try:
            archiving = _archiving_state(url)
        except Exception as exc:  # noqa: BLE001 — a probe, never a failure
            print(f"recovery point: could not determine WAL archiving state ({exc})")
        else:
            if archiving:
                print("recovery point: this dump plus continuous WAL archiving. "
                      "Prove the WAL half with `python scripts/pitr.py drill`.")
            else:
                print("recovery point: THIS DUMP ONLY — up to a full backup "
                      "interval of data would be lost. WAL archiving is off; "
                      "turn it on with `python scripts/pitr.py configure "
                      "--data-dir <PGDATA>`.")

    return 0


def _archiving_state(url: str) -> bool:
    """True when the server is archiving WAL, so PITR has something to replay."""
    import psycopg
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT setting FROM pg_settings WHERE name = 'archive_mode'").fetchone()
    return bool(row) and str(row[0]).lower() in {"on", "always"}


if __name__ == "__main__":
    raise SystemExit(main())
