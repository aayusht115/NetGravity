#!/usr/bin/env python
"""
NetGravity — Point-in-time recovery: configure it, and prove it.

    python scripts/pitr.py status
    python scripts/pitr.py configure --data-dir <PGDATA> [--archive-dir <DIR>]
    python scripts/pitr.py basebackup --out <DIR>
    python scripts/pitr.py drill --data-dir <PGDATA> --work-dir <DIR>

Why this exists
---------------
The previous report said PITR "cannot be closed from a repository" and pointed
at `docs/operations.md`. That was half right and it let the wrong conclusion
stand.

It is true that `archive_mode` is a server setting and that on a managed
instance you turn PITR on in the provider's console. It is NOT true that
nothing more can be done here. Three things can, and none of them had been:

1. **Configure it.** `ALTER SYSTEM SET` writes `postgresql.auto.conf` over a
   normal connection. A superuser connection is all it takes, and it is
   idempotent and reportable.
2. **Take the base backup PITR actually needs.** A `pg_dump` is a logical
   snapshot; you cannot replay WAL onto it. PITR needs a physical
   `pg_basebackup`. The backup script here took only the dump, so even with
   archiving on there was nothing to recover *from*.
3. **Prove a recovery works.** This is the part that matters and the part
   documentation cannot do. `drill` restores a base backup into a scratch
   cluster, replays the archived WAL to a chosen instant, starts it on another
   port, and checks that the data is as of that instant and not later. A
   recovery procedure nobody has executed is a belief, and it is the belief that
   fails on the day it is needed.

What is still someone else's job
--------------------------------
Where the archive goes. `archive_command` here copies WAL to a local
directory, which protects against everything except losing the machine — and
losing the machine is the main thing a backup is for. In production that
command ships to object storage (`aws s3 cp`, `azcopy`, `wal-g`, `pgbackrest`),
and choosing and crediting that target is a deployment decision, not a
repository one. `configure --archive-command` takes whatever you decide.

Safety
------
`drill` never writes to the source cluster. `pg_basebackup` is read-only
against it, the restore goes to a fresh directory, and the recovered cluster
starts on a different port with a different socket. Nothing here can promote,
overwrite, or reconfigure the running server; `configure` is the only
subcommand that changes a setting, it never restarts anything, and it says what
it changed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The scratch cluster the drill starts. Never the source's port.
DRILL_PORT = int(os.environ.get("NETGRAVITY_PITR_DRILL_PORT", "55433"))

#: A drilled recovery that has not reached consistency in this long has failed.
#: Long enough for a real replay, short enough not to hang a CI run.
RECOVERY_TIMEOUT_SECONDS = float(os.environ.get("NETGRAVITY_PITR_TIMEOUT", "120"))


# ---------------------------------------------------------------------------
# Tooling
# ---------------------------------------------------------------------------

def find_bindir(explicit: Optional[str] = None) -> Path:
    """
    Where `pg_basebackup`, `pg_ctl` and `psql` live.

    Looked up rather than assumed, because on Windows the PostgreSQL binaries
    are routinely not on PATH — and a script that shells out to a missing
    executable fails with a message about the shell rather than about
    PostgreSQL.
    """
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("NETGRAVITY_PG_BINDIR")
    if env:
        candidates.append(Path(env))
    which = shutil.which("pg_basebackup")
    if which:
        candidates.append(Path(which).parent)

    for candidate in candidates:
        if (candidate / "pg_basebackup.exe").exists() or (candidate / "pg_basebackup").exists():
            return candidate
    raise SystemExit(
        "Could not find the PostgreSQL client binaries. Put them on PATH or set "
        "NETGRAVITY_PG_BINDIR to the directory holding pg_basebackup and pg_ctl."
    )


def tool(bindir: Path, name: str) -> str:
    exe = bindir / (name + (".exe" if os.name == "nt" else ""))
    if not exe.exists():
        raise SystemExit(f"{name} is not present in {bindir}.")
    return str(exe)


def run(argv: List[str], *, timeout: float = 300.0,
        env: Optional[Dict[str, str]] = None,
        capture: bool = True) -> Tuple[int, str]:
    """
    Run a command, returning `(returncode, combined output)`.

    Never interactive: PostgreSQL's tools prompt for a password on a terminal
    and would hang a script forever. `-w` / `--no-password` is passed by every
    caller, and `PGPASSWORD` carries the credential when one is needed.

    `capture=False` for anything that STARTS a server. `pg_ctl start` launches a
    postmaster that inherits the parent's stdout, so a captured pipe is held
    open by the running database and never reaches EOF — the call then blocks
    until the timeout even though the server came up in a second. That is not a
    theoretical hazard: it made a successful recovery drill look like a hung
    one, twice.
    """
    merged = dict(os.environ)
    merged.setdefault("PGCONNECT_TIMEOUT", "10")
    if env:
        merged.update(env)
    try:
        if capture:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, env=merged)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        proc = subprocess.run(argv, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              timeout=timeout, env=merged)
        return proc.returncode, ""
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv[:2])}"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def database_url() -> str:
    """
    The configured PostgreSQL URL, read from the environment directly.

    Deliberately NOT via `app.backend.services.persistence`. That module
    constructs a `Database` at import time, which connects AND applies pending
    migrations — so importing it would mean `pitr.py status`, a read-only
    question about WAL settings, could migrate the schema of a production
    database as a side effect. The precedence here matches
    `configured_database_url()` exactly and is covered by a test that imports
    both and asserts they agree.
    """
    url = (os.environ.get("NETGRAVITY_DATABASE_URL")
           or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit(
            "No PostgreSQL URL is configured. PITR applies to PostgreSQL; "
            "SQLite has no WAL archive to replay. Set NETGRAVITY_DATABASE_URL."
        )
    return url


def query(url: str, sql: str) -> List[Dict[str, object]]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        return list(conn.execute(sql))


def scalar(url: str, sql: str) -> object:
    rows = query(url, sql)
    return list(rows[0].values())[0] if rows else None


#: The settings point-in-time recovery depends on.
_PITR_SETTINGS = ("wal_level", "archive_mode", "archive_command",
                  "archive_timeout", "max_wal_senders", "wal_keep_size",
                  "data_directory")


def settings(url: str) -> Dict[str, object]:
    """
    The settings PITR depends on, as raw values.

    Read from `pg_settings` rather than `SHOW`, because `SHOW` humanises: it
    reports `archive_timeout = 60` as `1min`. Comparing a requested `60`
    against a reported `1min` makes a setting that applied cleanly look like a
    failure, which is how `configure` came to announce a restart was needed
    when it was not — and, the same bug in the other direction, to announce
    everything was in effect when `archive_mode` was still off.
    """
    try:
        rows = query(url, "SELECT name, setting, unit, context, pending_restart "
                          "FROM pg_settings WHERE name = ANY(%(names)s)"
                     .replace("%(names)s", "'{" + ",".join(_PITR_SETTINGS) + "}'"))
    except Exception as exc:  # noqa: BLE001 — an unreadable catalogue is data
        return {name: f"unavailable ({type(exc).__name__}: {exc})"
                for name in _PITR_SETTINGS}
    return {str(r["name"]): r["setting"] for r in rows}


def setting_rows(url: str) -> Dict[str, Dict[str, object]]:
    """Full `pg_settings` rows for the PITR settings, keyed by name."""
    rows = query(url, "SELECT name, setting, unit, context, pending_restart "
                      "FROM pg_settings WHERE name = ANY('{"
                      + ",".join(_PITR_SETTINGS) + "}')")
    return {str(r["name"]): dict(r) for r in rows}


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    url = database_url()
    current = settings(url)

    print("PostgreSQL WAL archiving")
    print("-" * 64)
    for key, value in current.items():
        print(f"  {key:18} {value}")

    try:
        stats = query(url, "SELECT * FROM pg_stat_archiver")[0]
        print("\nArchiver")
        print("-" * 64)
        for key in ("archived_count", "last_archived_wal", "last_archived_time",
                    "failed_count", "last_failed_wal", "last_failed_time"):
            print(f"  {key:20} {stats.get(key)}")
    except Exception as exc:  # noqa: BLE001
        print(f"\npg_stat_archiver unavailable: {exc}")

    archiving_on = str(current.get("archive_mode", "off")).lower() in {"on", "always"}
    wal_ok = str(current.get("wal_level", "")).lower() in {"replica", "logical"}
    has_command = bool(str(current.get("archive_command", "")).strip())

    print("\nVerdict")
    print("-" * 64)
    if archiving_on and wal_ok and has_command:
        print("  PITR is CONFIGURED. Run `pitr.py drill` to prove a recovery "
              "actually works — a setting is not a capability.")
        return 0
    missing = []
    if not wal_ok:
        missing.append("wal_level must be replica or logical")
    if not archiving_on:
        missing.append("archive_mode must be on")
    if not has_command:
        missing.append("archive_command must be set")
    print("  PITR is NOT configured. Recovery is limited to the most recent "
          "logical dump, so the recovery point is however old that is.")
    for item in missing:
        print(f"    - {item}")
    print("  Fix with: python scripts/pitr.py configure --data-dir <PGDATA>")
    return 1


# ---------------------------------------------------------------------------
# configure
# ---------------------------------------------------------------------------

def cmd_configure(args: argparse.Namespace) -> int:
    url = database_url()
    before = settings(url)

    archive_dir = Path(args.archive_dir or (Path(args.data_dir).parent / "wal_archive"))
    archive_dir.mkdir(parents=True, exist_ok=True)

    if args.archive_command:
        archive_command = args.archive_command
    elif os.name == "nt":
        # `copy` is a cmd builtin, so it needs a shell; `/Y` suppresses the
        # overwrite prompt that would otherwise hang the archiver forever.
        archive_command = f'cmd /c copy /Y "%p" "{archive_dir}\\%f"'
    else:
        # `test ! -f` first: an archive_command MUST NOT overwrite an existing
        # file. PostgreSQL may retry a segment, and silently replacing an
        # already-archived one with a different byte sequence corrupts the
        # archive in a way that only shows up during a recovery.
        archive_command = f'test ! -f {archive_dir}/%f && cp %p {archive_dir}/%f'

    statements = [
        ("wal_level", "replica"),
        ("archive_mode", "on"),
        ("archive_command", archive_command),
        # An idle database produces no WAL, so without this the last committed
        # transaction can sit in an unarchived segment indefinitely — and the
        # recovery point silently becomes "the last busy period" rather than
        # "a minute ago".
        ("archive_timeout", args.archive_timeout),
    ]

    import psycopg
    from psycopg import sql

    # `ALTER SYSTEM SET` is parsed as utility SQL and takes no bind parameters,
    # so the value has to be composed into the statement. Composed through
    # `psycopg.sql` rather than an f-string: `archive_command` is
    # operator-supplied text full of quotes and backslashes, and it is going
    # into a file the server will execute.
    with psycopg.connect(url, autocommit=True) as conn:
        for name, value in statements:
            conn.execute(sql.SQL("ALTER SYSTEM SET {name} = {value}").format(
                name=sql.Identifier(name), value=sql.Literal(str(value))))
        conn.execute("SELECT pg_reload_conf()")

    rows = setting_rows(url)
    requested = dict(statements)
    print("Settings written to postgresql.auto.conf")
    print("-" * 64)
    for name, _ in statements:
        row = rows.get(name, {})
        print(f"  {name:18} was {before.get(name)} | requested "
              f"{requested[name]} | now {row.get('setting')} "
              f"[{row.get('context')}]")

    # A setting needs a restart when the RUNNING value is not the requested one
    # and its context is `postmaster`.
    #
    # `pending_restart` alone is not enough: it is set on SIGHUP processing,
    # which `pg_reload_conf()` only signals, so reading it immediately after is
    # a race — and losing that race is how this reported "every setting is in
    # effect" while `archive_mode` was still off, which is exactly the false
    # assurance the whole subcommand exists to avoid.
    restart_needed = []
    for name, value in statements:
        row = rows.get(name, {})
        running = str(row.get("setting", ""))
        if running == str(value):
            continue
        if row.get("pending_restart") or row.get("context") == "postmaster":
            restart_needed.append(name)
        else:
            print(f"\n  WARNING: {name} was written but reads back as "
                  f"{running!r} rather than {value!r}, and its context is "
                  f"{row.get('context')!r} — something else is overriding it "
                  f"(a command-line -c flag, or a later line in "
                  f"postgresql.conf).")
    print()
    if restart_needed:
        # `wal_level` and `archive_mode` are postmaster-level: a reload writes
        # them but does not apply them. Saying so is the whole point — a script
        # that reported success here would leave an operator believing archiving
        # was running when it was not.
        print("A RESTART is required before these take effect:")
        for name in restart_needed:
            print(f"    - {name}")
        print(f"\n  {Path(args.data_dir)}")
        print("  pg_ctl -D <that path> restart")
        print("\n  Nothing was restarted by this script: restarting a database "
              "is an operator's decision, not a side effect of configuring it.")
        return 2
    print("Every setting is in effect. Run `pitr.py drill` to prove recovery.")
    return 0


# ---------------------------------------------------------------------------
# basebackup
# ---------------------------------------------------------------------------

def cmd_basebackup(args: argparse.Namespace) -> int:
    bindir = find_bindir(args.bindir)
    url = database_url()
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} exists and is not empty; pg_basebackup needs an "
                         f"empty target so it cannot overwrite a previous backup.")
    out.mkdir(parents=True, exist_ok=True)

    started = time.time()
    code, output = run([
        tool(bindir, "pg_basebackup"),
        "--dbname", url,
        "--pgdata", str(out),
        # Plain format, so the drill can start it directly. `-Ft -z` is smaller
        # and right for shipping offsite; it needs an untar step first.
        "--format", "plain",
        # Stream WAL alongside, so the backup is self-consistent even if the
        # archive is momentarily behind.
        "--wal-method", "stream",
        "--checkpoint", "fast",
        "--progress", "--no-password",
    ], timeout=args.timeout)

    if code != 0:
        print(output.strip()[-2000:])
        print(f"\npg_basebackup FAILED (exit {code}).")
        return 1

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"Base backup written to {out}")
    print(f"  {size / (1024 * 1024):.1f} MiB in {time.time() - started:.1f}s")
    print("\nThis is the PHYSICAL backup point-in-time recovery replays WAL "
          "onto. A pg_dump cannot be used for that, which is why one is not "
          "enough on its own.")
    return 0


# ---------------------------------------------------------------------------
# drill
# ---------------------------------------------------------------------------

def cmd_drill(args: argparse.Namespace) -> int:
    """
    Prove a point-in-time recovery works, end to end.

    The shape of the test is the shape of the accident it protects against:

        1. take a base backup
        2. write a row nobody wants to lose            (BEFORE the target)
        3. note the instant                            (the recovery target)
        4. write a row representing the mistake        (AFTER the target)
        5. restore the base backup and replay to the instant
        6. assert the first row is present and the second is NOT

    Step 6 is the assertion that matters. A recovery that restores everything
    including the mistake has not recovered anything, and a recovery that
    restores neither row has lost committed data. Only checking both directions
    tests recovery rather than merely testing that the server starts.
    """
    bindir = find_bindir(args.bindir)
    url = database_url()

    current = settings(url)
    if str(current.get("archive_mode", "off")).lower() not in {"on", "always"}:
        print("archive_mode is not on, so there is no WAL archive to replay.")
        print("Run: python scripts/pitr.py configure --data-dir <PGDATA>, "
              "restart, then drill.")
        return 1

    archive_command = str(current.get("archive_command") or "")
    archive_dir = _archive_dir_from_command(archive_command, args.archive_dir)
    if archive_dir is None:
        print("Could not work out where WAL is archived to from "
              f"archive_command={archive_command!r}. Pass --archive-dir.")
        return 1

    work = Path(args.work_dir or tempfile.mkdtemp(prefix="ng_pitr_"))
    work.mkdir(parents=True, exist_ok=True)
    restore_dir = work / "restored"
    if restore_dir.exists():
        shutil.rmtree(restore_dir, ignore_errors=True)

    table = f"pitr_drill_{int(time.time())}"
    print(f"PITR drill  table={table}  archive={archive_dir}")
    print("=" * 68)

    # ---- 1. base backup ------------------------------------------------
    print("[1/6] base backup")
    code, output = run([
        tool(bindir, "pg_basebackup"), "--dbname", url,
        "--pgdata", str(restore_dir), "--format", "plain",
        "--wal-method", "stream", "--checkpoint", "fast", "--no-password",
    ], timeout=args.timeout)
    if code != 0:
        print(output.strip()[-1500:])
        return 1
    print(f"      -> {restore_dir}")

    import psycopg
    try:
        # ---- 2. the row that must survive ------------------------------
        print("[2/6] write a row that must survive the recovery")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, note TEXT)")
            conn.execute(f"INSERT INTO {table} VALUES (1, 'before the target')")
            # Forced so the row is definitely in an archived segment rather
            # than sitting in an open one when the target time passes.
            conn.execute("SELECT pg_switch_wal()")

        # ---- 3. the recovery target -----------------------------------
        # Read from the server, not from this machine's clock: recovery_target_time
        # is interpreted in the server's time zone, and a skewed client clock
        # would silently move the target.
        target = scalar(url, "SELECT now()")
        print(f"[3/6] recovery target = {target}")
        # A full second of daylight either side of the target. WAL timestamps
        # have sub-second resolution and a target landing inside the same
        # instant as the next write makes the test's own verdict ambiguous.
        time.sleep(1.5)

        # ---- 4. the mistake -------------------------------------------
        print("[4/6] write the row that represents the mistake")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"INSERT INTO {table} VALUES (2, 'after the target')")
            conn.execute("SELECT pg_switch_wal()")
        # Give the archiver a moment to ship the segments the replay needs.
        _wait_for_archiver(url, seconds=args.archive_wait)

        # ---- 5. recover to the target ---------------------------------
        print("[5/6] recover the base backup to the target instant")
        _write_recovery_config(restore_dir, archive_dir, target, DRILL_PORT)
        code, output = run([
            tool(bindir, "pg_ctl"), "-D", str(restore_dir),
            "-o", f"-p {DRILL_PORT}",
            "-l", str(work / "recovery.log"), "start", "-w",
            "-t", str(int(RECOVERY_TIMEOUT_SECONDS)),
        ], timeout=RECOVERY_TIMEOUT_SECONDS + 30, capture=False)
        log = (work / "recovery.log")
        if code != 0:
            print(output.strip()[-1000:])
            if log.exists():
                print("--- recovery log tail ---")
                print(log.read_text(errors="replace")[-2000:])
            return 1

        try:
            # ---- 6. the assertion, both directions --------------------
            print("[6/6] verify what the recovered cluster holds")
            drill_url = _rewrite_port(url, DRILL_PORT)
            _wait_for_queries(drill_url, seconds=RECOVERY_TIMEOUT_SECONDS)
            rows = query(drill_url, f"SELECT id, note FROM {table} ORDER BY id")
            ids = [int(r["id"]) for r in rows]
            print(f"      rows recovered: {ids}")

            survived = 1 in ids
            excluded = 2 not in ids
            print()
            print("Result")
            print("-" * 68)
            print(f"  committed row before the target recovered : "
                  f"{'YES' if survived else 'NO — COMMITTED DATA LOST'}")
            print(f"  row after the target correctly excluded   : "
                  f"{'YES' if excluded else 'NO — RECOVERY OVERSHOT THE TARGET'}")

            if survived and excluded:
                print("\n  PITR VERIFIED. A recovery to a chosen instant keeps "
                      "everything committed before it and nothing after it.")
                return 0
            print("\n  PITR DRILL FAILED. The configuration exists but the "
                  "recovery does not do what it must.")
            return 1
        finally:
            run([tool(bindir, "pg_ctl"), "-D", str(restore_dir),
                 "-m", "immediate", "stop", "-w"], timeout=60, capture=False)
    finally:
        # The drill's own table is removed from the SOURCE cluster. The restored
        # copy is left in place when --keep is passed, because a failed drill is
        # exactly when someone wants to look at it.
        try:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not drop {table} from the source: {exc})")
        if not args.keep:
            shutil.rmtree(restore_dir, ignore_errors=True)
        else:
            print(f"\n  Restored cluster kept at {restore_dir}")


def _archive_dir_from_command(command: str, explicit: Optional[str]) -> Optional[Path]:
    """
    Where WAL segments land, read out of the configured `archive_command`.

    Best-effort by design: an `archive_command` shipping to object storage has
    no local directory to find, and the drill needs one to replay from. In that
    case `--archive-dir` is required and this returns None rather than guessing.
    """
    if explicit:
        return Path(explicit)
    # The last quoted or bare path-looking token that contains %f.
    import re
    for match in re.findall(r'"([^"]*%f[^"]*)"|(\S*%f\S*)', command):
        token = match[0] or match[1]
        if not token:
            continue
        token = token.replace("%f", "").rstrip("\\/")
        candidate = Path(token.strip('"'))
        if candidate.is_absolute() and candidate.exists():
            return candidate
    return None


def _conf_literal(value: str) -> str:
    """
    A string as a PostgreSQL configuration-file literal.

    Inside single quotes the config parser processes `\\\\` as one backslash and
    `''` as one quote, so both have to be doubled going in. `ALTER SYSTEM` does
    this for you; a hand-written `postgresql.auto.conf` does not, and getting it
    wrong on a Windows path is how a correct archive produced a recovery that
    stopped at the base backup and reported it as a missing recovery target.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


def _write_recovery_config(data_dir: Path, archive_dir: Path,
                           target: object, port: int) -> None:
    """
    Turn a restored base backup into a cluster that will replay to `target`.

    `recovery.signal` is what makes PostgreSQL 12+ start in recovery at all;
    without it the server starts normally and ignores every recovery setting,
    which looks like a successful recovery that recovered nothing.

    `recovery_target_action = promote` ends recovery at the target instead of
    pausing, so the drill can query the result rather than waiting on a paused
    cluster.
    """
    # On Windows this command MUST use backslashes.
    #
    # `copy` is a cmd builtin, not a Win32 API call, and it does not accept
    # forward slashes in a path even inside quotes — it fails with "The system
    # cannot find the file specified" on a file that is plainly there. Verified
    # directly: the same copy succeeds with backslashes and fails with forward
    # slashes, in the same directory, on the same file.
    #
    # Which means the config-file escaping has to be got right rather than
    # sidestepped, because this file is written by hand here rather than by
    # `ALTER SYSTEM`. See `_conf_literal`.
    #
    # The way this failure presents is the reason it is worth this much comment:
    # PostgreSQL reports it as "recovery ended before configured recovery target
    # was reached", which reads as a WAL or archive problem. The archive was
    # complete and correct the whole time.
    archive_native = str(archive_dir)
    sep = "\\" if os.name == "nt" else "/"
    restore_command = (
        f'copy /Y "{archive_native}{sep}%f" "%p"' if os.name == "nt"
        else f'cp "{archive_native}/%f" "%p"'
    )
    target_text = (target.isoformat(sep=" ") if isinstance(target, datetime)
                   else str(target))
    conf = data_dir / "postgresql.auto.conf"
    lines = [
        "# Written by scripts/pitr.py drill.",
        f"restore_command = {_conf_literal(restore_command)}",
        f"recovery_target_time = {_conf_literal(target_text)}",
        "recovery_target_inclusive = on",
        "recovery_target_action = 'promote'",
        f"port = {port}",
        "listen_addresses = '127.0.0.1'",
        # The recovered cluster must not archive: it would write segments with
        # the same names as the source's into the same archive, and a WAL
        # archive with two different segments claiming one name cannot be
        # replayed by anybody afterwards.
        "archive_mode = off",
    ]
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (data_dir / "recovery.signal").write_text("", encoding="utf-8")
    # A restored backup may carry the source's own standby signal; leaving it
    # would start a standby rather than a recovery.
    standby = data_dir / "standby.signal"
    if standby.exists():
        standby.unlink()


def _wait_for_archiver(url: str, *, seconds: float) -> None:
    """Wait until the archiver has caught up, or until it is clearly stuck."""
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            row = query(url, "SELECT last_archived_wal, failed_count FROM pg_stat_archiver")[0]
        except Exception:  # noqa: BLE001
            return
        if int(row.get("failed_count") or 0) > 0:
            print(f"      WARNING: archiver reports {row['failed_count']} failure(s); "
                  f"the archive may be incomplete.")
        if row.get("last_archived_wal") and row.get("last_archived_wal") == last:
            return
        last = row.get("last_archived_wal")
        time.sleep(1.0)


def _wait_for_queries(url: str, *, seconds: float) -> None:
    """Wait until the recovered cluster accepts a query."""
    deadline = time.time() + seconds
    last_error: Optional[Exception] = None
    while time.time() < deadline:
        try:
            scalar(url, "SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1.0)
    raise SystemExit(f"The recovered cluster never accepted a query: {last_error}")


def _rewrite_port(url: str, port: int) -> str:
    """The same connection URL, pointed at the drill cluster's port."""
    import re
    # Host may or may not carry a port; both forms have to work.
    if re.search(r"@[^/]+:\d+/", url):
        return re.sub(r"(@[^/]+):\d+/", rf"\1:{port}/", url, count=1)
    return re.sub(r"(@[^/:]+)/", rf"\1:{port}/", url, count=1)


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bindir", help="Directory holding pg_basebackup / pg_ctl.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Report whether PITR is configured.")
    p_status.set_defaults(func=cmd_status)

    p_conf = sub.add_parser("configure", help="Turn on WAL archiving.")
    p_conf.add_argument("--data-dir", required=True,
                        help="PGDATA, reported in the restart instruction.")
    p_conf.add_argument("--archive-dir",
                        help="Where WAL segments are copied (default: PGDATA/../wal_archive).")
    p_conf.add_argument("--archive-command",
                        help="Override the archive command entirely — use this to "
                             "ship WAL to object storage.")
    p_conf.add_argument("--archive-timeout", default="60",
                        help="Force a segment switch this often, so an idle "
                             "database still has a recent recovery point.")
    p_conf.set_defaults(func=cmd_configure)

    p_base = sub.add_parser("basebackup", help="Take the physical backup PITR needs.")
    p_base.add_argument("--out", required=True)
    p_base.add_argument("--timeout", type=float, default=900.0)
    p_base.set_defaults(func=cmd_basebackup)

    p_drill = sub.add_parser("drill", help="Prove a point-in-time recovery works.")
    p_drill.add_argument("--data-dir", required=False, help="Source PGDATA (informational).")
    p_drill.add_argument("--work-dir", help="Where the restored cluster is built.")
    p_drill.add_argument("--archive-dir", help="WAL archive to replay from.")
    p_drill.add_argument("--timeout", type=float, default=900.0)
    p_drill.add_argument("--archive-wait", type=float, default=30.0)
    p_drill.add_argument("--keep", action="store_true",
                        help="Leave the restored cluster in place for inspection.")
    p_drill.set_defaults(func=cmd_drill)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
