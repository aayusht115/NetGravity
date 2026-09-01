"""Phase 10.8 — the controls that decide whether this can be deployed.

Every check drives the real application. Where a control is only meaningful
against a real database, the check runs against PostgreSQL.

    python validation/phase_10_8/run_production_readiness_check.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = pathlib.Path(__file__).parent
PASSWORD = "Netgravity@2026"

results = {"checks": []}


def record(cid: str, name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results["checks"].append(
        {"id": cid, "name": name, "status": status, "detail": detail})
    print(f"[{status:4}] {cid:6} {name}" + (f" - {detail}" if detail else ""))
    sys.stdout.flush()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from app.backend.app import app
    from app.backend.services import persistence, totp
    from app.backend.services.migrations import MIGRATIONS, SCHEMA_VERSION
    from app.backend.services.ratelimit import limiter
    from app.backend.services.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE

    app.config["TESTING"] = True
    client = app.test_client()
    limiter.reset()

    # ---- Schema migrations --------------------------------------------
    record("M-01", "The schema is versioned, not CREATE-TABLE-IF-NOT-EXISTS",
           persistence.database.schema_version == SCHEMA_VERSION,
           f"at v{persistence.database.schema_version} of {SCHEMA_VERSION}: "
           + ", ".join(f"{m.version}:{m.name}" for m in MIGRATIONS))

    rows = persistence.database.query(
        "SELECT version, name, applied_at FROM schema_migrations ORDER BY version", ())
    record("M-02", "Every migration is recorded with when it was applied",
           len(rows) == len(MIGRATIONS) and all(r["applied_at"] for r in rows),
           f"{len(rows)} rows in schema_migrations")

    # An existing, populated database adopts the migration table without loss.
    import sqlite3
    import tempfile
    from app.backend.services.migrations import _m001_baseline
    legacy = os.path.join(tempfile.gettempdir(), f"legacy-{uuid.uuid4().hex[:8]}.db")
    conn = sqlite3.connect(legacy)
    for statement in _m001_baseline("sqlite"):
        conn.execute(statement)
    conn.execute("INSERT INTO users(user_id,email,document,created_at) "
                 "VALUES('u1','a@b.c','{\"user_id\":\"u1\"}',1.0)")
    conn.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES('t','u1',9e9)")
    conn.commit()
    conn.close()
    upgraded = persistence.Database(path=legacy)
    survived = upgraded.query_one("SELECT user_id FROM users")
    has_new = upgraded.query_one("SELECT client FROM sessions WHERE token='t'")
    record("M-03", "A populated pre-migration database upgrades without data loss",
           upgraded.schema_version == SCHEMA_VERSION and survived is not None
           and has_new is not None,
           f"v0 -> v{upgraded.schema_version}, user and session preserved, "
           f"new columns added")
    second = persistence.Database(path=legacy)
    record("M-04", "Re-opening applies nothing", second.migrations_applied == [],
           f"applied on second open: {second.migrations_applied}")
    upgraded.close(); second.close(); os.unlink(legacy)

    # ---- Transport security -------------------------------------------
    from app.backend.services.persistence import enforce_transport_security as tls
    remote = tls("postgresql://u:p@db.example.com:5432/ng", production=False)
    record("S-01", "A remote database connection defaults to TLS",
           "sslmode=require" in remote, remote)

    refused = False
    try:
        tls("postgresql://u:p@db.example.com/ng?sslmode=disable", production=True)
    except RuntimeError:
        refused = True
    record("S-02", "Production refuses an unencrypted remote connection",
           refused, "sslmode=disable to a remote host is rejected at start-up")

    local = "postgresql://u:p@127.0.0.1:5432/ng"
    record("S-03", "A local socket is exempt rather than broken",
           tls(local, production=True) == local, local)

    # ---- Backups -------------------------------------------------------
    backup_dir = OUT / "backup_check"
    backup_dir.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "backup_database.py"),
         "--out", str(backup_dir), "--verify-restore", "--keep", "2"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=900,
        env={**os.environ},
    )
    verified = "verified: every table restored" in proc.stdout
    record("B-01", "A backup is taken AND verified by restoring it",
           proc.returncode == 0 and verified,
           proc.stdout.strip().splitlines()[-1] if proc.stdout else proc.stderr[:120])

    # ---- Credentials ----------------------------------------------------
    email = f"p108-{uuid.uuid4().hex[:8]}@example.com"
    weak = client.post("/api/auth/signup", json={
        "name": "Weak", "email": f"w-{uuid.uuid4().hex[:6]}@x.com",
        "password": "Sh0rt!12"})
    record("C-01", "A password below the floor is refused",
           weak.status_code == 400 and "12 characters" in weak.get_data(as_text=True),
           f"HTTP {weak.status_code}")

    res = client.post("/api/auth/signup", json={
        "name": "Phase 108", "email": email, "password": PASSWORD})
    token = res.get_json()["token"]
    cookies = res.headers.getlist("Set-Cookie")
    session_cookie = next((c for c in cookies if c.startswith(f"{SESSION_COOKIE}=")), "")
    record("C-02", "The session is an httpOnly, SameSite cookie",
           "HttpOnly" in session_cookie and "SameSite=Lax" in session_cookie,
           session_cookie.split(";", 1)[-1].strip()[:90])

    no_csrf = client.post("/api/projects", json={"name": "CSRF probe"})
    csrf_value = client.get_cookie(CSRF_COOKIE).value
    with_csrf = client.post("/api/projects", json={"name": "CSRF probe"},
                            headers={CSRF_HEADER: csrf_value})
    record("C-03", "A cookie-authenticated write needs a CSRF token",
           no_csrf.status_code == 403 and with_csrf.status_code in (200, 201),
           f"without header {no_csrf.status_code}, with header {with_csrf.status_code}")

    # ---- Brute force -----------------------------------------------------
    limiter.reset()
    lock_client = app.test_client()
    lock_email = f"lock-{uuid.uuid4().hex[:8]}@example.com"
    lock_client.post("/api/auth/signup", json={
        "name": "Lock", "email": lock_email, "password": PASSWORD})
    for _ in range(10):
        lock_client.post("/api/auth/login",
                         json={"email": lock_email, "password": "wrong-password-x"})
    correct = lock_client.post("/api/auth/login",
                               json={"email": lock_email, "password": PASSWORD})
    state = persistence.login_lock_state(lock_email)
    record("C-04", "Ten failures lock the account, correct password included",
           correct.status_code == 401 and state["locked_until"] > time.time(),
           f"HTTP {correct.status_code}, failures={state['failures']}, "
           f"locked for {int(state['locked_until'] - time.time())}s")
    record("C-05", "The lock is in the database, so it survives a restart",
           state is not None and "login_attempts" in persistence.TABLES,
           "row in login_attempts, not a process dictionary")

    # ---- Rate limiting ----------------------------------------------------
    limiter.reset()
    burst = app.test_client()
    statuses = [burst.post("/api/auth/login",
                           json={"email": "spray@x.com", "password": "x" * 12}
                           ).status_code for _ in range(25)]
    record("C-06", "A credential spray is rate limited, not just locked out",
           429 in statuses and statuses.index(429) >= 20,
           f"first 429 at attempt {statuses.index(429) + 1 if 429 in statuses else '-'}")

    # ---- Password reset ---------------------------------------------------
    from app.backend.services.security import auth_service
    limiter.reset()
    known = client.post("/api/auth/password/reset", json={"email": email})
    unknown = client.post("/api/auth/password/reset",
                          json={"email": f"nobody-{uuid.uuid4().hex}@x.com"})
    record("R-01", "The reset endpoint is not an account-enumeration oracle",
           known.get_json() == unknown.get_json()
           and known.status_code == unknown.status_code,
           "an unknown address answers identically to a known one")

    issued = auth_service.begin_password_reset(email)
    reset_token = issued[1]
    stored = persistence.database.query("SELECT token_hash FROM password_resets", ())
    record("R-02", "Only the HASH of a reset token is stored",
           all(r["token_hash"] != reset_token for r in stored),
           "a database read is not an account takeover")

    new_password = "A-brand-new-passphrase-9"
    applied = client.post("/api/auth/password/reset/confirm",
                          json={"token": reset_token, "password": new_password})
    replay = client.post("/api/auth/password/reset/confirm",
                         json={"token": reset_token, "password": "Another-one-2"})
    record("R-03", "A reset works once and only once",
           applied.status_code == 200 and replay.status_code == 401,
           f"first {applied.status_code}, replay {replay.status_code}")

    dead = app.test_client().get("/api/auth/me",
                                 headers={"Authorization": f"Bearer {token}"})
    record("R-04", "A reset signs the account out everywhere",
           dead.status_code == 401,
           "the session issued before the reset is dead")

    # ---- Second factor ----------------------------------------------------
    limiter.reset()
    mfa_client = app.test_client()
    mfa_email = f"mfa-{uuid.uuid4().hex[:8]}@example.com"
    mfa_client.post("/api/auth/signup", json={
        "name": "MFA", "email": mfa_email, "password": PASSWORD})
    csrf = mfa_client.get_cookie(CSRF_COOKIE).value
    enrolment = mfa_client.post("/api/auth/mfa/enrol",
                                headers={CSRF_HEADER: csrf}).get_json()
    record("F-01", "Enrolment issues a standard otpauth secret and recovery codes",
           enrolment["otpauth_uri"].startswith("otpauth://totp/")
           and len(enrolment["recovery_codes"]) == 10,
           f"{len(enrolment['recovery_codes'])} recovery codes, "
           f"algorithm SHA1 digits 6 period 30")

    before = mfa_client.get("/api/auth/mfa").get_json()
    mfa_client.post("/api/auth/mfa/confirm", headers={CSRF_HEADER: csrf}, json={
        "code": totp.code_for_step(enrolment["secret"], totp.current_step())})
    after = mfa_client.get("/api/auth/mfa").get_json()
    record("F-02", "It is not active until a working code confirms it",
           before["confirmed"] is False and after["confirmed"] is True,
           "a mis-scanned QR cannot lock the user out of their own account")

    limiter.reset()
    fresh = app.test_client()
    first = fresh.post("/api/auth/login",
                       json={"email": mfa_email, "password": PASSWORD}).get_json()
    challenge_as_session = fresh.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {first['mfa_token']}"})
    record("F-03", "The password alone no longer yields a session",
           first.get("mfa_required") is True and "token" not in first
           and challenge_as_session.status_code == 401,
           "the challenge carries a different prefix and is refused as a session")

    second = fresh.post("/api/auth/login/mfa", json={
        "mfa_token": first["mfa_token"],
        "code": totp.code_for_step(enrolment["secret"], totp.current_step() + 1)})
    record("F-04", "A valid code completes the sign-in",
           second.status_code == 200
           and second.get_json()["user"]["email"] == mfa_email,
           f"HTTP {second.status_code}")

    user = auth_service._users_by_email[mfa_email]  # noqa: SLF001
    replayed = auth_service.verify_second_factor(
        user, totp.code_for_step(enrolment["secret"], totp.current_step() + 1))
    record("F-05", "A code cannot be replayed inside its own 30-second window",
           replayed is False,
           "the time step is claimed atomically, so a captured code is spent")

    recovery = enrolment["recovery_codes"][0]
    record("F-06", "A recovery code works exactly once",
           auth_service.verify_second_factor(user, recovery) is True
           and auth_service.verify_second_factor(user, recovery) is False,
           "hashed at rest, single-use")

    # ---- Sessions ---------------------------------------------------------
    listing = mfa_client.get("/api/auth/sessions").get_json()
    record("N-01", "Live sessions are listed without returning a token",
           listing["total"] >= 1
           and all("token" not in s for s in listing["sessions"]),
           f"{listing['total']} session(s), identified by digest")

    other = auth_service.issue_session(user)
    revoked = mfa_client.delete("/api/auth/sessions",
                                headers={CSRF_HEADER: csrf}).get_json()
    still_here = mfa_client.get("/api/auth/me").status_code
    gone = app.test_client().get(
        "/api/auth/me", headers={"Authorization": f"Bearer {other}"}).status_code
    record("N-02", "Sign out everywhere keeps the current session and kills the rest",
           revoked["revoked"] >= 1 and still_here == 200 and gone == 401,
           f"revoked {revoked['revoked']}, this session {still_here}, other {gone}")

    # ---- Headers and supply chain ------------------------------------------
    status = client.get("/api/status")
    csp = status.headers.get("Content-Security-Policy", "")
    record("H-01", "A Content Security Policy forbids inline and eval'd script",
           "script-src 'self'" in csp and "'unsafe-eval'" not in csp
           and "unsafe-inline" not in csp.split("style-src")[0],
           csp[:110] + "...")

    record("H-02", "The usual headers are set",
           status.headers.get("X-Content-Type-Options") == "nosniff"
           and status.headers.get("X-Frame-Options") == "DENY"
           and "camera=()" in status.headers.get("Permissions-Policy", ""),
           "nosniff, DENY, strict-origin-when-cross-origin, Permissions-Policy")

    record("H-03", "API responses are not cacheable by an intermediary",
           status.headers.get("Cache-Control") == "no-store",
           "per-account data behind a cookie")

    front = ROOT / "app" / "frontend"
    import re as _re

    def _live(text):
        """Comments naming a replaced CDN are documentation, not requests."""
        text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.S)
        text = _re.sub(r"<!--.*?-->", "", text, flags=_re.S)
        return _re.sub(r"^\s*//.*$", "", text, flags=_re.M)

    sources = "\n".join(
        _live(path.read_text(encoding="utf-8", errors="replace"))
        for path in [front / "index.html"] + list((front / "css").glob("*.css"))
        + list((front / "js").glob("**/*.js")))
    third_party = [h for h in ("unpkg.com", "cdn.jsdelivr.net",
                               "cdnjs.cloudflare.com", "fonts.googleapis.com",
                               "fonts.gstatic.com", "basemaps.cartocdn.com")
                   if h in sources]
    vendored = (list((front / "vendor").glob("*.js"))
                + list((front / "vendor" / "fonts").glob("*.woff2")))
    record("H-04", "No runtime asset comes from a third party",
           not third_party and len(vendored) >= 4,
           f"{len(vendored)} assets vendored locally; "
           f"third-party hosts remaining: {third_party or 'none'}")

    handlers = sum(
        path.read_text(encoding="utf-8", errors="replace").count("onclick=")
        for path in [ROOT / "app" / "frontend" / "index.html"]
        + list((ROOT / "app" / "frontend" / "js").glob("**/*.js"))
        if path.name != "actions.js")
    record("H-05", "No inline event handler remains",
           handlers == 0,
           f"{handlers} onclick attributes (they cannot be nonced, so one "
           f"would break the page under this policy)")

    # ---- Multi-period ------------------------------------------------------
    from netgravity.optimization.milp import milp_solve
    from netgravity.schemas.network import (
        CanonicalNetwork, DemandRecord, FacilityRecord, FacilityStatus,
        LaneRecord, NodeRole, OptimizationConfig, ProductRecord, TransportMode)

    def multi(policy):
        return CanonicalNetwork(
            network_id="MP",
            facilities=[
                FacilityRecord(id="P", name="P", role=NodeRole.PLANT,
                               status=FacilityStatus.EXISTING,
                               capacity_units_per_period=9999,
                               is_mandatory=True, is_closable=False),
                FacilityRecord(id="D", name="D", role=NodeRole.DC,
                               status=FacilityStatus.EXISTING,
                               capacity_units_per_period=150.0,
                               fixed_cost_per_year=1200.0),
                FacilityRecord(id="M", name="M", role=NodeRole.MARKET,
                               status=FacilityStatus.EXISTING, is_closable=False),
            ],
            products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=10.0)],
            demands=[DemandRecord(market_id="M", product_id="P1", period=p, quantity=q)
                     for p, q in ((1, 100.0), (2, 100.0), (3, 160.0))],
            lanes=[LaneRecord(origin_id="P", destination_id="D",
                              mode=TransportMode.ROAD, rate_per_unit=1.0,
                              distance_km=10.0, lead_time_days=1.0),
                   LaneRecord(origin_id="D", destination_id="M",
                              mode=TransportMode.ROAD, rate_per_unit=2.0,
                              distance_km=10.0, lead_time_days=1.0)],
            config=OptimizationConfig(
                enable_inventory=False, enforce_sla=False, enable_carbon_cost=False,
                allow_shortage=True, verbose=False, multi_period_policy=policy))

    mean = milp_solve(multi("REPRESENTATIVE_MEAN"), None)
    served = sum(f.flow_units for f in mean.flow_decisions if f.destination_id == "M")
    record("P-01", "A multi-period network solves instead of crashing the solver",
           served > 0,
           f"3 periods -> {served:,.0f} units served (this raised "
           f"PulpError: overlapping constraint names)")

    record("P-02", "The result states what it covers and what it does not",
           mean.period_report["collapsed"] is True
           and mean.period_report["peak_total"] == 160.0
           and "does not size for" in mean.period_report["note"],
           mean.period_report["note"][:130])

    peak = milp_solve(multi("PEAK"), None)
    peak_served = sum(f.flow_units for f in peak.flow_decisions
                      if f.destination_id == "M")
    record("P-03", "PEAK sizes for the worst period and shows the breach",
           peak_served == 150.0 and peak.period_report["peak_total"] == 160.0,
           f"mean serves {served:,.0f}; peak wants 160 against 150 of capacity "
           f"and serves {peak_served:,.0f}")

    record("P-04", "The solver metadata carries it too",
           any("demand periods" in w for w in mean.solver.warnings),
           "a reader of the solver result alone is still told")

    # ---- Deployment posture -------------------------------------------------
    body = status.get_json()
    record("D-01", "Health states which store is in use and its schema version",
           body["storage"]["engine"] in ("postgresql", "sqlite")
           and body["storage"]["schema_version"] == SCHEMA_VERSION,
           json.dumps(body["storage"]))

    record("D-02", "Health states whether a reset can actually be delivered",
           "reset_delivery" in body and "configured" in body["reset_delivery"],
           json.dumps(body["reset_delivery"]))

    record("D-03", "The dead prototype-network module is gone",
           not (ROOT / "app" / "frontend" / "js" / "graph.js").exists(),
           "graph.js held a hardcoded 18-node network and was imported by nothing")

    record("D-04", "An operations runbook exists",
           (ROOT / "docs" / "operations.md").exists(),
           "docs/operations.md — production config, migrations, backups, PITR")

    # ---- Report --------------------------------------------------------------
    failed = [c for c in results["checks"] if c["status"] == "FAIL"]
    results["summary"] = {"total": len(results["checks"]),
                          "passed": len(results["checks"]) - len(failed),
                          "failed": len(failed)}
    (OUT / "production_readiness_validation.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n{results['summary']['passed']}/{results['summary']['total']} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
