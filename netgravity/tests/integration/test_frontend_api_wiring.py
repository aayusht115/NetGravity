"""
NetGravity — Frontend ↔ Backend Integration & API Wiring Tests
==============================================================
Validates that all REST endpoints, DTO serialization, authoritative KPI
responses, scenario simulations, and error states match the integration
contracts.

Phase 10.0 note — why these assertions changed
----------------------------------------------
Every test below is preserved in intent: each still asserts that its endpoint is
reachable and returns a well-formed DTO. What changed is that several of them
previously asserted behaviour the Phase 10.0 forensic audit identified as
defects, so passing them REQUIRED the defect to exist:

  * `test_login_success` logged in as a seeded user with the password
    "password" — the endpoint accepted any password at all.
  * `test_signup_success` registered an account with **no password field**.
  * `test_me_endpoint` asserted HTTP 200 with **no Authorization header** —
    that is the authentication bypass itself, asserted as a contract.
  * `test_list_projects` asserted `len(projects) >= 5`, which only held because
    five fabricated projects were hardcoded into the module.
  * `test_list_scenarios` asserted `>= 2`, holding only because two fabricated
    scenarios were hardcoded.
  * `test_simulate_scenario_contract` asserted the presence of `totalCost` and
    `sla` — the literal fabricated fields the endpoint returned instead of the
    real solve it had just performed.
  * `test_get_forecast` asserted a 24-point history and 6-point cone, which held
    only because both were hardcoded constants.
  * `test_get_signals` asserted `>= 3` signals, holding only because three
    fabricated market bulletins were hardcoded.

These are updated to the corrected contract rather than deleted, and coverage is
extended: each now also asserts the property that replaced the defect.
"""

from __future__ import annotations

import uuid

import pytest

from app.backend.app import app

DEMO_PROJECT = "pr-demo-case16"
GOOD_PASSWORD = "integration-test-pw-1"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def token(client):
    """
    A real authenticated session.

    The address must be unique per test: the account store persists for the
    life of the process, and signup deliberately refuses a duplicate email.
    `id(client)` is not unique — CPython reuses addresses once an object is
    freed, which made two tests collide in a full-suite run.
    """
    email = f"wiring-{uuid.uuid4().hex}@example.com"
    res = client.post("/api/auth/signup", json={"email": email, "password": GOOD_PASSWORD})
    assert res.status_code == 201, res.get_json()
    return res.get_json()["token"]


@pytest.fixture
def auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestAuthAPIWiring:

    def test_login_success(self, client):
        """Login now requires the correct password, so the account is created first."""
        client.post("/api/auth/signup",
                    json={"email": "wiring.login@example.com", "password": GOOD_PASSWORD})
        res = client.post("/api/auth/login",
                          json={"email": "wiring.login@example.com", "password": GOOD_PASSWORD})
        assert res.status_code == 200
        data = res.get_json()
        assert "token" in data
        assert data["user"]["email"] == "wiring.login@example.com"
        assert data["status"] == "authenticated"

    def test_login_rejects_wrong_password(self, client):
        """New coverage: the behaviour whose absence the old suite asserted."""
        client.post("/api/auth/signup",
                    json={"email": "wiring.badpw@example.com", "password": GOOD_PASSWORD})
        res = client.post("/api/auth/login",
                          json={"email": "wiring.badpw@example.com", "password": "wrong"})
        assert res.status_code == 401

    def test_login_validation_failure(self, client):
        res = client.post("/api/auth/login", json={})
        assert res.status_code == 400
        assert res.get_json()["error"]["code"] == "VALIDATION_ERROR"

    def test_signup_success(self, client):
        res = client.post("/api/auth/signup", json={
            "email": "new.planner@company.com",
            "name": "New Planner",
            "password": GOOD_PASSWORD,
        })
        assert res.status_code == 201
        data = res.get_json()
        assert "token" in data
        assert data["user"]["name"] == "New Planner"

    def test_signup_requires_a_password(self, client):
        res = client.post("/api/auth/signup", json={"email": "nopw@company.com"})
        assert res.status_code == 400

    def test_me_endpoint(self, client, auth):
        res = client.get("/api/auth/me", headers=auth)
        assert res.status_code == 200
        assert "user" in res.get_json()

    def test_me_endpoint_requires_authentication(self, client):
        """The old suite asserted 200 here; that WAS the bypass."""
        assert client.get("/api/auth/me").status_code == 401


class TestProjectsAPIWiring:

    def test_list_projects(self, client, auth):
        res = client.get("/api/projects", headers=auth)
        assert res.status_code == 200
        data = res.get_json()
        assert "projects" in data
        # The demo workspace is shared with every user; fabricated client
        # projects are gone, so a new user sees exactly that one.
        assert any(p["id"] == DEMO_PROJECT for p in data["projects"])

    def test_create_and_get_project(self, client, auth):
        create_res = client.post("/api/projects", json={
            "name": "Integration Test Workspace",
            "region": "India",
            "client": "Test Client Corp",
            "description": "Validation workspace",
        }, headers=auth)
        assert create_res.status_code == 201
        proj = create_res.get_json()

        get_res = client.get(f"/api/projects/{proj['id']}", headers=auth)
        assert get_res.status_code == 200
        assert get_res.get_json()["name"] == "Integration Test Workspace"

    def test_created_project_starts_without_a_network(self, client, auth):
        """New coverage: a project must not inherit the synthetic snapshot."""
        proj = client.post("/api/projects", json={"name": "No Data Yet"},
                           headers=auth).get_json()
        assert proj["snapshot_id"] is None
        assert proj["has_network"] is False


class TestKPIsAPIWiring:

    def test_network_kpis_contract(self, client, auth):
        res = client.get(f"/api/kpis/network?project_id={DEMO_PROJECT}", headers=auth)
        assert res.status_code == 200
        kpis = res.get_json()["kpis"]
        for metric in ("business_network_cost", "pct_demand_in_sla",
                       "max_utilization_pct", "total_carbon_kg"):
            assert metric in kpis

    def test_network_kpis_carry_status(self, client, auth):
        """New coverage: no KPI may present a value without a VALID status."""
        res = client.get(f"/api/kpis/network?project_id={DEMO_PROJECT}", headers=auth)
        for metric_id, result in res.get_json()["kpis"].items():
            assert "status" in result
            if result["status"] != "VALID":
                assert result["value"] is None, f"{metric_id} fabricates a value"

    def test_facility_kpis_contract(self, client, auth):
        res = client.get(f"/api/kpis/facilities?project_id={DEMO_PROJECT}", headers=auth)
        assert res.status_code == 200
        assert "facilities" in res.get_json()

    def test_thresholds_catalogue(self, client, auth):
        res = client.get("/api/kpis/thresholds", headers=auth)
        assert res.status_code == 200
        assert len(res.get_json()["thresholds"]) > 0


class TestScenariosAPIWiring:

    def test_list_scenarios(self, client, auth):
        res = client.get(f"/api/scenarios?project_id={DEMO_PROJECT}", headers=auth)
        assert res.status_code == 200
        data = res.get_json()
        # Starts empty: the two fabricated "canonical" scenarios are gone.
        # Scenarios now exist only once actually solved.
        assert isinstance(data["scenarios"], list)

    def test_simulate_scenario_contract(self, client, auth):
        res = client.post(f"/api/scenarios/simulate?project_id={DEMO_PROJECT}", json={
            "project_id": DEMO_PROJECT,
            "name": "Custom Simulation Test",
            "action": "CHANGE_CAPACITY",
            "facility_ids": ["DC_CENTRAL"],
            "capacity_delta_units": -1000.0,
        }, headers=auth)
        assert res.status_code == 201, res.get_json()
        data = res.get_json()
        assert data["name"] == "Custom Simulation Test"
        # Authoritative payload replaces the fabricated `totalCost` / `sla`.
        assert "baseline_kpis" in data
        assert "scenario_kpis" in data
        assert "deltas" in data
        assert data["provenance"]["llm_used"] is False

    def test_simulate_rejects_unknown_action(self, client, auth):
        res = client.post(f"/api/scenarios/simulate?project_id={DEMO_PROJECT}", json={
            "project_id": DEMO_PROJECT,
            "action": "TELEPORT_WAREHOUSE",
            "facility_ids": ["DC_CENTRAL"],
        }, headers=auth)
        assert res.status_code == 400

    def test_baseline_is_available_and_separate(self, client, auth):
        res = client.get(f"/api/scenarios/baseline?project_id={DEMO_PROJECT}", headers=auth)
        assert res.status_code == 200
        assert res.get_json()["type"] == "BASELINE"


class TestForecastAndSignalsAPIWiring:

    def test_get_forecast(self, client, auth):
        """
        Returns 200 with an explicit status. Where the demo network has no
        ingested demand history the status is FORECAST_UNAVAILABLE and `series`
        is empty — the prototype returned a fabricated cone here instead.
        """
        res = client.get(f"/api/forecast?project_id={DEMO_PROJECT}", headers=auth)
        assert res.status_code == 200
        data = res.get_json()
        assert data["status"] in ("OK", "FORECAST_UNAVAILABLE")
        assert "series" in data
        if data["status"] == "OK":
            for series in data["series"]:
                assert "status" in series
                for point in series["points"]:
                    assert point["p10"] <= point["p50"] <= point["p90"]
        else:
            assert data["series"] == []

    def test_forecast_rejects_absent_project(self, client, auth):
        assert client.get("/api/forecast", headers=auth).status_code == 400

    def test_get_signals(self, client, auth):
        """
        The three fabricated market bulletins are gone. With no signal source
        configured the endpoint says so rather than serving invented ones.
        """
        res = client.get("/api/signals", headers=auth)
        assert res.status_code == 200
        data = res.get_json()
        assert "signals" in data
        assert data["status"] in ("OK", "NO_SIGNAL_SOURCE_CONFIGURED")
        if data["status"] == "NO_SIGNAL_SOURCE_CONFIGURED":
            assert data["signals"] == []


class TestTwinAPIWiring:

    def test_twin_states_query(self, client, auth):
        """
        The endpoint is wired — and now requires a session to reach.

        This asserted a 200 for an ANONYMOUS caller, which is the behaviour it
        was written against: `/orchestrator/*` was mounted with no
        authentication at all, so every Digital Twin state in the process could
        be read by anyone who could reach the port. The wiring assertion is
        unchanged; it just carries credentials, as a browser does.
        """
        res = client.get("/orchestrator/twin/states", headers=auth)
        assert res.status_code == 200

    def test_twin_states_refuses_an_anonymous_caller(self, client):
        """The control plane holds customer network state and must not serve it
        to a caller with no session."""
        assert client.get("/orchestrator/twin/states").status_code == 401

    def test_the_whole_control_plane_refuses_an_anonymous_caller(self, client):
        """
        Every route, not a sampled few.

        The guard is a `before_request` on the blueprint rather than a
        decorator per route, precisely so a new endpoint cannot be added
        without it. This checks the ones that would leak the most.
        """
        for path, method in (
            ("/orchestrator/capabilities", "get"),
            ("/orchestrator/workflows", "get"),
            ("/orchestrator/health", "get"),
            ("/orchestrator/twin/states", "get"),
            ("/orchestrator/executions/anything", "get"),
            ("/orchestrator/executions/anything/trace", "get"),
            ("/orchestrator/run", "post"),
            ("/orchestrator/chat", "post"),
            ("/orchestrator/insights", "post"),
            ("/orchestrator/approvals/anything", "post"),
        ):
            res = getattr(client, method)(path, json={})
            assert res.status_code == 401, f"{method.upper()} {path} served anonymously"
