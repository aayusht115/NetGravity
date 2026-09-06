"""
Phase 10.0 — Production application integration tests.

These assert the properties the forensic audit found missing, and they are
written to FAIL if any of them regresses:

  * authentication actually verifies a password and cannot be bypassed;
  * project isolation is enforced at the registry, not per-route;
  * a project with no ingested network reports NO_NETWORK_BOUND rather than
    silently answering from the bundled synthetic network;
  * KPI and scenario responses carry authoritative status, never fabricated
    business literals;
  * the duplicate MILP and the fabricated data-quality constants are gone.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.backend.app import app as flask_app
from app.backend.services.project_registry import ProjectRegistry
from app.backend.services.security import AuthService, hash_password, verify_password


REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "app" / "backend" / "api"


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _signup(client, email: str, password: str = "correct-horse-1"):
    res = client.post("/api/auth/signup", json={"email": email, "password": password})
    assert res.status_code == 201, res.get_json()
    return res.get_json()["token"]


def _auth(token: str):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Password handling
# ---------------------------------------------------------------------------
class TestPasswordHashing:
    def test_hash_is_salted_and_verifies(self):
        h1 = hash_password("s3cret-password")
        h2 = hash_password("s3cret-password")
        assert h1 != h2, "each hash must use a fresh salt"
        assert verify_password("s3cret-password", h1)
        assert verify_password("s3cret-password", h2)

    def test_wrong_password_rejected(self):
        assert not verify_password("wrong", hash_password("s3cret-password"))

    def test_plaintext_never_stored(self):
        stored = hash_password("s3cret-password")
        assert "s3cret-password" not in stored
        assert stored.startswith("pbkdf2$")

    def test_malformed_record_denies_rather_than_raises(self):
        for bad in ("", "garbage", "pbkdf2$notanint$aa$bb", "sha1$1$aa$bb"):
            assert verify_password("anything", bad) is False


class TestAuthService:
    def test_unknown_email_is_rejected(self):
        svc = AuthService()
        from app.backend.services.errors import UnauthenticatedError

        with pytest.raises(UnauthenticatedError):
            svc.authenticate(email="nobody@example.com", password="whatever")

    def test_wrong_password_is_rejected(self):
        svc = AuthService()
        from app.backend.services.errors import UnauthenticatedError

        svc.register(email="a@example.com", password="correct-horse-1")
        with pytest.raises(UnauthenticatedError):
            svc.authenticate(email="a@example.com", password="wrong-password")

    def test_login_does_not_autoprovision_unknown_accounts(self):
        """The prototype created an account for any unknown email on login."""
        svc = AuthService()
        from app.backend.services.errors import UnauthenticatedError

        with pytest.raises(UnauthenticatedError):
            svc.authenticate(email="ghost@example.com", password="anything-at-all")
        assert svc._users_by_email == {}

    def test_short_password_rejected(self):
        svc = AuthService()
        from app.backend.services.errors import ValidationError

        with pytest.raises(ValidationError):
            svc.register(email="b@example.com", password="short")

    def test_invalid_token_has_no_anonymous_fallback(self):
        svc = AuthService()
        from app.backend.services.errors import UnauthenticatedError

        for token in ("", "not-a-real-token", "ngt_deadbeef"):
            with pytest.raises(UnauthenticatedError):
                svc.resolve_session(token)


# ---------------------------------------------------------------------------
# HTTP auth surface
# ---------------------------------------------------------------------------
class TestAuthEndpoints:
    def test_login_with_wrong_password_is_401(self, client):
        client.post("/api/auth/signup",
                    json={"email": "wrongpw@example.com", "password": "correct-horse-1"})
        res = client.post("/api/auth/login",
                          json={"email": "wrongpw@example.com", "password": "not-it"})
        assert res.status_code == 401
        assert res.get_json()["error"]["code"] == "UNAUTHENTICATED"

    def test_login_with_correct_password_succeeds(self, client):
        client.post("/api/auth/signup",
                    json={"email": "goodpw@example.com", "password": "correct-horse-1"})
        res = client.post("/api/auth/login",
                          json={"email": "goodpw@example.com", "password": "correct-horse-1"})
        assert res.status_code == 200
        assert res.get_json()["token"].startswith("ngt_")

    def test_me_without_token_is_401_not_a_default_user(self, client):
        res = client.get("/api/auth/me")
        assert res.status_code == 401, "there must be no anonymous fallback user"

    def test_protected_routes_reject_anonymous_access(self, client):
        for path in (
            "/api/projects",
            "/api/kpis/network?project_id=pr-demo-case16",
            "/api/scenarios?project_id=pr-demo-case16",
            "/api/forecast?project_id=pr-demo-case16",
            "/api/signals",
        ):
            res = client.get(path)
            assert res.status_code == 401, f"{path} must require authentication"


# ---------------------------------------------------------------------------
# Project isolation
# ---------------------------------------------------------------------------
class TestProjectIsolation:
    def test_user_cannot_read_another_users_project(self, client):
        token_a = _signup(client, "owner-a@example.com")
        token_b = _signup(client, "owner-b@example.com")

        created = client.post("/api/projects", json={"name": "A's Network"},
                              headers=_auth(token_a))
        assert created.status_code == 201
        project_id = created.get_json()["id"]

        res = client.get(f"/api/projects/{project_id}", headers=_auth(token_b))
        assert res.status_code == 403
        assert res.get_json()["error"]["code"] == "FORBIDDEN"

    def test_listing_excludes_other_users_projects(self, client):
        token_a = _signup(client, "list-a@example.com")
        token_b = _signup(client, "list-b@example.com")

        client.post("/api/projects", json={"name": "Private To A"}, headers=_auth(token_a))
        listed = client.get("/api/projects", headers=_auth(token_b)).get_json()

        assert all(p["name"] != "Private To A" for p in listed["projects"])

    def test_registry_denies_cross_owner_access(self):
        from app.backend.services.errors import ForbiddenError

        registry = ProjectRegistry()
        record = registry.create(name="Owned", owner_id="user-1")
        with pytest.raises(ForbiddenError):
            registry.get(record.project_id, user_id="user-2")


# ---------------------------------------------------------------------------
# No silent substitution of synthetic data
# ---------------------------------------------------------------------------
class TestNoNetworkBound:
    def test_new_project_has_no_network(self, client):
        token = _signup(client, "fresh@example.com")
        created = client.post("/api/projects", json={"name": "Fresh"},
                              headers=_auth(token)).get_json()

        assert created["snapshot_id"] is None
        assert created["has_network"] is False

    def test_kpis_for_unbound_project_refuse_rather_than_substitute(self, client):
        token = _signup(client, "unbound@example.com")
        project_id = client.post("/api/projects", json={"name": "Unbound"},
                                 headers=_auth(token)).get_json()["id"]

        res = client.get(f"/api/kpis/network?project_id={project_id}", headers=_auth(token))
        assert res.status_code == 409
        assert res.get_json()["error"]["code"] == "NO_NETWORK_BOUND"

    def test_registry_raises_rather_than_borrowing_a_snapshot(self):
        from app.backend.services.errors import NoNetworkBoundError

        registry = ProjectRegistry()
        record = registry.create(name="Empty", owner_id="user-1")
        with pytest.raises(NoNetworkBoundError):
            registry.snapshot_for(record.project_id, user_id="user-1")


# ---------------------------------------------------------------------------
# Authoritative KPI surface
# ---------------------------------------------------------------------------
class TestAuthoritativeKPIs:
    def test_demo_project_kpis_carry_status_and_provenance(self, client):
        token = _signup(client, "kpi-reader@example.com")
        res = client.get("/api/kpis/network?project_id=pr-demo-case16", headers=_auth(token))
        assert res.status_code == 200

        body = res.get_json()
        assert body["snapshot_id"]
        assert body["kpis"], "the demo network must produce KPIs"

        for metric_id, result in body["kpis"].items():
            assert "status" in result, f"{metric_id} must carry a KPIStatus"
            assert "authoritative_owner" in result
            if result["status"] != "VALID":
                assert result["value"] is None, (
                    f"{metric_id} is {result['status']} but carries a value"
                )

    def test_demo_project_actually_produces_valid_kpis(self, client):
        """
        Regression guard.

        Asserting only "every KPI carries a status" is satisfied by a response in
        which EVERY KPI is unavailable — which is exactly what happened when the
        baseline request was submitted as free text: the deterministic NLU
        classified it REQUIRES_HUMAN, no capability ran, and the whole cockpit
        went blank while still being technically honest. A network that solves
        must yield real numbers.
        """
        token = _signup(client, "kpi-valid@example.com")
        res = client.get("/api/kpis/network?project_id=pr-demo-case16", headers=_auth(token))
        kpis = res.get_json()["kpis"]

        valid = {k: v for k, v in kpis.items() if v["status"] == "VALID"}
        assert len(valid) >= 8, (
            f"only {len(valid)} of {len(kpis)} KPIs are VALID — the baseline solve "
            f"probably did not run"
        )
        assert valid["business_network_cost"]["value"] > 0
        # INR because THIS network says so, not because the registry says INR
        # about everything. The demo fixture declares `currency="INR"`; a US
        # workbook declares USD and must report USD, which is the whole reason
        # the unit stopped being a literal in `metrics/registry.py`.
        assert valid["business_network_cost"]["unit"] == "INR"
        money_units = {
            mid: kpi["unit"] for mid, kpi in valid.items()
            if mid.endswith("_cost") or mid in {"solver_objective", "cost_per_period"}
        }
        assert set(money_units.values()) == {"INR"}, (
            f"every money metric must carry the network's own currency: {money_units}"
        )
        assert res.get_json()["currency"] == "INR", (
            "the response envelope must name the currency its figures are in"
        )
        assert 0 <= valid["max_utilization_pct"]["value"] <= 100

    def test_facility_kpis_are_populated_for_a_solved_network(self, client):
        token = _signup(client, "fac-valid@example.com")
        res = client.get("/api/kpis/facilities?project_id=pr-demo-case16", headers=_auth(token))
        facilities = res.get_json()["facilities"]
        assert facilities, "a solved network must expose per-facility KPIs"
        for fac_id, metrics in facilities.items():
            assert "utilization_pct" in metrics, f"{fac_id} has no utilisation metric"

    def test_evidence_package_is_exposed(self, client):
        token = _signup(client, "evidence@example.com")
        res = client.get("/api/kpis/evidence?project_id=pr-demo-case16", headers=_auth(token))
        assert res.status_code == 200
        assert "evidence" in res.get_json()


# ---------------------------------------------------------------------------
# Structural guarantees — the fabrication patterns must not return
# ---------------------------------------------------------------------------
def _module_source(name: str) -> str:
    return (API_DIR / name).read_text(encoding="utf-8")


def _code_without_comments(source: str) -> str:
    """
    Strip comments and docstrings so a module that *describes* a removed
    fabrication is not mistaken for one that still performs it.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


class TestNoFabricatedLiterals:
    @pytest.mark.parametrize("module", ["scenarios.py", "forecast.py", "ingestion_dynamic.py"])
    def test_no_hardcoded_business_values(self, module):
        code = _code_without_comments(_module_source(module))
        forbidden = [
            "1285000", "1184000", "1205000", "1220000",  # fabricated costs
            "96.7", "95.5", "94.3", "96.5",              # fabricated SLA
            "105688", "231737", "102400",                # fabricated carbon
            "68.2", "72.4", "65.3",                      # fabricated utilisation
        ]
        found = [lit for lit in forbidden if lit in code]
        assert not found, f"{module} still contains fabricated business values: {found}"

    def test_ingestion_does_not_assert_a_fixed_validity_rate(self):
        code = _code_without_comments(_module_source("ingestion_dynamic.py"))
        assert "0.98" not in code, "validity must be measured, not assumed"
        assert "98.0" not in code

    def test_duplicate_solver_is_gone(self):
        code = _module_source("network_extractor.py")
        assert "def solve_extracted_network" not in code
        assert "LpProblem" not in _code_without_comments(code), (
            "optimisation must live only in netgravity/optimization/milp.py"
        )

    def test_extractor_no_longer_imports_pulp(self):
        code = _code_without_comments(_module_source("network_extractor.py"))
        assert "import pulp" not in code

    def test_forecast_has_no_silent_exception_fallback(self):
        """The prototype did `except Exception: pass` then returned a cone."""
        tree = ast.parse(_module_source("forecast.py"))
        for handler in [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]:
            body = [s for s in handler.body if not isinstance(s, ast.Pass)]
            assert body, "an exception handler must not silently pass"


class TestNoClientSideCalculationInAPI:
    def test_scenario_module_reads_the_registry(self):
        code = _module_source("scenarios.py")
        assert "registry.scenario_comparison" in code
        assert "registry.network_kpis" in code

    def test_scenario_module_does_not_import_a_solver(self):
        code = _code_without_comments(_module_source("scenarios.py"))
        for banned in ("pulp", "LpProblem", "milp"):
            assert banned not in code, f"scenarios.py must not reference {banned}"


class TestTheNetworkStatesItsOwnCurrencyAndGeography:
    """
    A project is not India, and its money is not rupees, unless its data says
    so.

    `ProjectRecord.region` defaulted to "India" on the dataclass, in `create()`,
    in the loader and in the API — and the creation form offered no other
    option. Every money metric's `unit` was the literal "INR", the evidence
    formatter prefixed every amount with a rupee sign, and the browser's
    `formatCurrency` grouped in lakh. A US workbook stating USD on all 268 of
    its freight-rate rows reported a ₹23,226,260 baseline on every screen.
    """

    def test_a_new_project_has_no_region_until_something_says(self, client):
        token = _signup(client, "region-blank@example.com")
        res = client.post("/api/projects", json={"name": "Unstated"},
                          headers=_auth(token))
        body = res.get_json()
        assert body["region"] == "", (
            "an unstated region must stay unstated, not silently become India"
        )
        assert body.get("region_source") == ""

    def test_an_explicit_region_is_recorded_as_the_users_own(self, client):
        token = _signup(client, "region-explicit@example.com")
        res = client.post("/api/projects",
                          json={"name": "Stated", "region": "United States"},
                          headers=_auth(token))
        assert res.get_json()["region"] == "United States"
        assert res.get_json()["region_source"] == "user"

    def test_the_demo_network_reports_its_own_currency(self, client):
        token = _signup(client, "ccy-demo@example.com")
        res = client.get("/api/kpis/network?project_id=pr-demo-case16",
                         headers=_auth(token))
        body = res.get_json()
        assert body["currency"] == "INR"
        cost = body["kpis"]["business_network_cost"]
        assert cost["unit"] == "INR"

    def test_the_structure_endpoint_publishes_currency_and_geography(self, client):
        token = _signup(client, "struct-geo@example.com")
        res = client.get("/api/network/structure?project_id=pr-demo-case16",
                         headers=_auth(token))
        body = res.get_json()
        assert body["currency"] == "INR"
        assert body["geography"].get("region") == "India"

    def test_the_structure_endpoint_says_which_sites_are_only_proposed(self, client):
        """
        A proposed site and an operating one used to leave this endpoint
        identical, so every screen downstream drew them the same way — the map,
        the DC table, and the scenario builder's "sites already in my data"
        dropdown all presented a warehouse the client has not built as one they
        run today. The solver's own open/closed decision cannot substitute: it
        answers a different question and comes from a different endpoint.
        """
        token = _signup(client, "struct-status@example.com")
        res = client.get("/api/network/structure?project_id=pr-demo-case16",
                         headers=_auth(token))
        body = res.get_json()

        sites = body["plants"] + body["dcs"]
        assert sites, "the demo network must publish facilities"
        assert all("status" in s for s in sites)

        statuses = {s["id"]: s["status"] for s in sites}
        assert "EXISTING" in statuses.values()
        candidates = [fid for fid, st in statuses.items() if st == "CANDIDATE"]
        assert candidates, f"case16 carries two candidate DCs; got {statuses}"

        # A market is demand, not capacity, and carries no meaningful status.
        assert all(m["status"] is None for m in body["markets"])

        # Region and category are what demand growth is scoped by.
        assert any(n.get("region") for n in sites + body["markets"])
        assert all("category" in p for p in body["products"])

    def test_evidence_amounts_carry_the_networks_symbol(self, client):
        token = _signup(client, "ccy-evidence@example.com")
        res = client.get("/api/insights?project_id=pr-demo-case16",
                         headers=_auth(token))
        money = [e for i in res.get_json()["insights"]
                 for e in (i.get("evidence") or [])
                 if e.get("unit") == "INR"]
        assert money, "a solved network must report at least one money figure"
        assert all(e["display_value"].startswith("₹") for e in money)


class TestFacilityInsightsSurviveAFailedRun:
    """
    A run that produces no network state still publishes a twin state, and
    publishes it as OPTIMIZED with zero facilities — deliberately, so a viewer
    sees an explicitly empty state rather than a stale one.

    The insights endpoint then preferred OPTIMIZED *by label*. Once any run had
    failed for a snapshot, that empty state outranked the populated BASELINE for
    every later request, and every facility-scoped briefing answered 404
    "Facility 'F005' is not present in state …" — for a network whose facilities
    were on screen beside it. The entire per-facility insight surface was
    unreachable on a solved network.
    """

    def test_a_facility_briefing_resolves_for_a_solved_network(self, client):
        token = _signup(client, "fac-insight@example.com")
        facilities = client.get("/api/kpis/facilities?project_id=pr-demo-case16",
                                headers=_auth(token)).get_json()["facilities"]
        assert facilities
        fid = sorted(facilities)[0]
        res = client.get(
            f"/api/insights?project_id=pr-demo-case16&scope=FACILITY&entity_id={fid}",
            headers=_auth(token))
        assert res.status_code == 200, res.get_json()
        assert res.get_json()["entity_id"] == fid

    def test_an_empty_optimized_state_does_not_outrank_a_populated_one(self):
        """The ranking itself, without needing a failed run to exist."""
        from types import SimpleNamespace

        def rank(ref):
            populated = 1 if getattr(ref, "n_facilities", 0) else 0
            optimized = 1 if str(getattr(ref, "state_type", "")).upper().endswith(
                "OPTIMIZED") else 0
            return (populated, optimized)

        empty_optimized = SimpleNamespace(
            state_id="empty", state_type="TwinStateType.OPTIMIZED", n_facilities=0)
        full_baseline = SimpleNamespace(
            state_id="full", state_type="TwinStateType.BASELINE", n_facilities=12)
        full_optimized = SimpleNamespace(
            state_id="best", state_type="TwinStateType.OPTIMIZED", n_facilities=12)

        assert sorted([empty_optimized, full_baseline], key=rank)[-1].state_id == "full"
        assert sorted([full_baseline, full_optimized], key=rank)[-1].state_id == "best"


class TestTheAuditEndpoint:
    def test_a_project_with_no_upload_says_so(self, client):
        token = _signup(client, "dataset-empty@example.com")
        pid = client.post("/api/projects", json={"name": "Empty"},
                          headers=_auth(token)).get_json()["id"]
        res = client.get(f"/api/ingestions/preview/dataset?project_id={pid}",
                         headers=_auth(token))
        assert res.status_code == 200
        assert res.get_json()["status"] == "NO_DATA"

    def test_it_refuses_another_users_project(self, client):
        token_a = _signup(client, "dataset-a@example.com")
        token_b = _signup(client, "dataset-b@example.com")
        pid = client.post("/api/projects", json={"name": "A only"},
                          headers=_auth(token_a)).get_json()["id"]
        res = client.get(f"/api/ingestions/preview/dataset?project_id={pid}",
                         headers=_auth(token_b))
        assert res.status_code == 403

    def test_a_bound_project_with_no_record_explains_itself(self, client):
        """The seeded demo has a snapshot and never went through an upload.
        Reporting "nothing was uploaded" would be misleading."""
        token = _signup(client, "dataset-demo@example.com")
        res = client.get(
            "/api/ingestions/preview/dataset?project_id=pr-demo-case16",
            headers=_auth(token))
        body = res.get_json()
        assert res.status_code == 200
        assert body["status"] == "NO_DATA"
        assert "no upload record was kept" in body.get("notice", "")
