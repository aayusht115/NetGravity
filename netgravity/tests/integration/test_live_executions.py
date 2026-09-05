"""
Watching an execution while it is still running.

Every other view of an orchestrator execution is a view of a finished one:
``/executions/<id>`` and ``/executions/<id>/trace`` both need an execution id,
and a client only learns that id from the response — which arrives when the
work is already over. So the twenty to forty seconds of a cold solve were,
from the browser's side, silent, and the loading screen could say nothing true
about them beyond "still waiting".

``GET /orchestrator/executions/live`` closes that gap. It reads the live
``ExecutionContext`` — the state machine's position, the outcome of each
capability so far, and the completed, failed and blocked step lists — keyed on
the id the BROWSER put on its own HTTP request (``X-Request-ID``), which the
API layer files each execution under as ``<correlation>:<purpose>``.

What these tests protect:

  * the correlation prefix, because two executions started by one request must
    not collide on an idempotency key;
  * the route's contract, including that an unknown correlation is an empty
    list rather than an error — the first poll of a request routinely happens
    before the execution exists;
  * that it is scoped to the caller, so an authenticated user holding someone
    else's correlation id cannot read the shape of their run;
  * and that it reports a real execution's real capabilities.
"""

from __future__ import annotations

import uuid

import pytest

from app.backend.app import app as flask_app
from app.backend.services.correlation import (
    CORRELATION_HEADER,
    client_correlation_id,
    matches_correlation,
    orchestrator_request_id,
)


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _signup(client, email: str, password: str = "Netgravity@2026") -> str:
    res = client.post("/api/auth/signup", json={"name": "T", "email": email,
                                                "password": password})
    assert res.status_code in (200, 201), res.get_json()
    return res.get_json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _a_project(client, token: str):
    res = client.get("/api/projects", headers=_auth(token))
    assert res.status_code == 200, res.get_json()
    rows = res.get_json().get("projects") or []
    for row in rows:
        if row.get("has_network"):
            return row["id"]
    return None


class TestTheCorrelationKey:
    """The browser's id for its request, turned into an idempotency key."""

    def test_no_request_context_means_a_fresh_uuid(self):
        value = orchestrator_request_id("kpi-baseline")
        # A UUID, not an empty string and not the purpose on its own: with no
        # header to correlate against, this must behave exactly as it did
        # before correlation existed.
        assert uuid.UUID(value)

    def test_the_browsers_id_becomes_the_prefix(self):
        with flask_app.test_request_context(
                "/api/kpis/network", headers={CORRELATION_HEADER: "req_abc123"}):
            assert client_correlation_id() == "req_abc123"
            assert orchestrator_request_id("kpi-baseline") == "req_abc123:kpi-baseline"

    def test_two_executions_from_one_request_do_not_collide(self):
        """
        `request_id` is the idempotency key: two executions filed under the
        same one make the second resolve to the first and return the wrong
        answer. One HTTP request can start more than one execution, so the
        purpose is part of the key.
        """
        with flask_app.test_request_context(
                "/x", headers={CORRELATION_HEADER: "req_abc123"}):
            a = orchestrator_request_id("kpi-baseline")
            b = orchestrator_request_id("kpi-resilience")
        assert a != b
        assert matches_correlation(a, "req_abc123")
        assert matches_correlation(b, "req_abc123")

    def test_a_header_that_is_not_an_id_is_ignored(self):
        """This value is echoed back to clients and used as a key. Anything
        that is not id-shaped falls back to a UUID rather than being trusted."""
        for bad in ("a b", "x" * 200, "<script>", "req/../etc"):
            with flask_app.test_request_context("/x", headers={CORRELATION_HEADER: bad}):
                assert client_correlation_id() == ""
                assert uuid.UUID(orchestrator_request_id("p"))

    def test_a_prefix_is_a_whole_segment(self):
        assert matches_correlation("req_abc:kpi", "req_abc")
        assert matches_correlation("req_abc", "req_abc")
        # Not a substring match: `req_abc` must not claim `req_abcd`'s run.
        assert not matches_correlation("req_abcd:kpi", "req_abc")
        assert not matches_correlation("", "req_abc")
        assert not matches_correlation("req_abc:kpi", "")


class TestTheLiveRoute:
    def test_it_requires_a_session(self, client):
        res = client.get("/orchestrator/executions/live?correlation_id=req_x")
        assert res.status_code == 401, res.get_json()

    def test_a_missing_correlation_id_is_refused(self, client):
        token = _signup(client, f"live.{uuid.uuid4().hex[:8]}@kearney.com")
        res = client.get("/orchestrator/executions/live", headers=_auth(token))
        assert res.status_code == 400
        assert res.get_json()["error"]["code"] == "VALIDATION_FAILURE"

    def test_an_over_long_correlation_id_is_refused(self, client):
        token = _signup(client, f"live.{uuid.uuid4().hex[:8]}@kearney.com")
        res = client.get("/orchestrator/executions/live?correlation_id=" + "x" * 200,
                         headers=_auth(token))
        assert res.status_code == 400

    def test_an_unknown_correlation_is_an_empty_list(self, client):
        """
        NOT a 404. The first poll of a request routinely lands before the
        execution has been created, and an error there would make the loading
        screen stop watching a run that was about to start.
        """
        token = _signup(client, f"live.{uuid.uuid4().hex[:8]}@kearney.com")
        res = client.get("/orchestrator/executions/live?correlation_id=req_nobody",
                         headers=_auth(token))
        assert res.status_code == 200
        body = res.get_json()
        assert body["correlation_id"] == "req_nobody"
        assert body["executions"] == []


class TestItReportsARealExecution:
    """One genuine solve, watched through the route."""

    def test_it_reports_the_capabilities_that_actually_ran(self, client):
        token = _signup(client, f"live.{uuid.uuid4().hex[:8]}@kearney.com")
        project_id = _a_project(client, token)
        if not project_id:
            pytest.skip("no seeded project with a network to solve")

        correlation = "req_" + uuid.uuid4().hex[:12]
        headers = dict(_auth(token))
        headers[CORRELATION_HEADER] = correlation
        solved = client.get(f"/api/scenarios/baseline?project_id={project_id}",
                            headers=headers)
        if solved.status_code != 200:
            pytest.skip(f"the baseline solve did not run: {solved.status_code}")

        res = client.get(f"/orchestrator/executions/live?correlation_id={correlation}",
                         headers=_auth(token))
        assert res.status_code == 200
        execs = res.get_json()["executions"]
        assert execs, "the execution the request started was not found"

        run = execs[0]
        assert run["request_id"].startswith(correlation)
        assert run["state"], "no state machine position"
        # Capabilities, from the registry's own names — not a list this test
        # or the frontend invented.
        assert run["capability_status"], run
        assert all(isinstance(k, str) and isinstance(v, str)
                   for k, v in run["capability_status"].items())
        assert "optimization.solve" in run["capability_status"], run["capability_status"]
        for key in ("planned_capabilities", "completed_steps", "failed_steps",
                    "blocked_steps", "skipped_steps", "errors"):
            assert isinstance(run[key], list), key

    def test_another_user_cannot_read_it(self, client):
        """
        Executions are indexed process-wide. A correlation id is unguessable,
        but "unguessable" is not an authorisation check.
        """
        owner = _signup(client, f"live.{uuid.uuid4().hex[:8]}@kearney.com")
        project_id = _a_project(client, owner)
        if not project_id:
            pytest.skip("no seeded project with a network to solve")

        correlation = "req_" + uuid.uuid4().hex[:12]
        headers = dict(_auth(owner))
        headers[CORRELATION_HEADER] = correlation
        solved = client.get(f"/api/scenarios/baseline?project_id={project_id}",
                            headers=headers)
        if solved.status_code != 200:
            pytest.skip(f"the baseline solve did not run: {solved.status_code}")

        assert client.get(
            f"/orchestrator/executions/live?correlation_id={correlation}",
            headers=_auth(owner)).get_json()["executions"], "the owner cannot see it"

        stranger = _signup(client, f"live.{uuid.uuid4().hex[:8]}@kearney.com")
        res = client.get(f"/orchestrator/executions/live?correlation_id={correlation}",
                         headers=_auth(stranger))
        assert res.status_code == 200
        assert res.get_json()["executions"] == [], "another user read this run"
