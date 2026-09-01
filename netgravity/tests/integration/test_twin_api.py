"""
Phase 5 — the Digital Twin HTTP surface.

Thin by design: each endpoint delegates to `DigitalTwinService` and serialises
the result. These tests check the wiring, the paging contract and the status
codes; the twin's behaviour is covered by `test_digital_twin.py`.

The surface exists so a future visualisation frontend can read state without
coupling the core to a UI framework — so what is asserted here is that
everything a viewer needs (state, provenance, paging, comparison) is reachable
over HTTP and JSON-serialisable.
"""

from __future__ import annotations

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.api import create_orchestrator_blueprint
from netgravity.orchestrator.schemas.requests import Actor, ActorRole
from netgravity.orchestrator.schemas.requests import (
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)

from .conftest import build_delhi_network

flask = pytest.importorskip("flask", reason="Flask is required for the HTTP surface")


@pytest.fixture
def wired():
    """An orchestrator that has run one scenario, plus a client onto it."""
    from flask import Flask

    orchestrator = build_orchestrator(network=build_delhi_network(), enable_llm=False)
    response = orchestrator.run_sync(OrchestratorRequest(
        input="What if we close DC_DELHI?",
        explicit_intent=Intent.SCENARIO_ANALYSIS,
        explicit_scenarios=[ScenarioIntentSpec(
            action=ScenarioActionType.CLOSE_FACILITY, facility_ids=["DC_DELHI"],
        )],
    ))

    app = Flask(__name__)
    # The blueprint FAILS CLOSED without an authenticator: it was mounted
    # with no authentication at all, so every control-plane endpoint was
    # open to anyone who could reach the process. These tests exercise chat
    # and twin behaviour, so they supply a fixed test identity rather than
    # an authentication layer.
    app.register_blueprint(create_orchestrator_blueprint(
        orchestrator,
        authenticator=lambda: Actor(actor_id='test-user', role=ActorRole.PLANNER),
    ))
    app.config.update(TESTING=True)
    return app.test_client(), orchestrator, response


class TestStateListing:

    def test_states_are_listed_as_handles(self, wired):
        client, _, response = wired
        result = client.get("/orchestrator/twin/states")

        assert result.status_code == 200
        states = result.get_json()["states"]
        assert len(states) == 2
        assert {s["state_type"] for s in states} == {"OPTIMIZED", "SCENARIO"}
        # Handles, not payloads.
        assert all("facilities" not in s for s in states)

    def test_listing_can_be_filtered_by_snapshot(self, wired):
        client, _, response = wired
        result = client.get(
            f"/orchestrator/twin/states?snapshot_id={response.network_snapshot_id}"
        )
        assert len(result.get_json()["states"]) == 2

        empty = client.get("/orchestrator/twin/states?snapshot_id=snap_nope")
        assert empty.get_json()["states"] == []


class TestStateRetrieval:

    def test_a_state_is_readable_by_id(self, wired):
        client, _, response = wired
        state_id = response.twin_states[0]["state_id"]
        result = client.get(f"/orchestrator/twin/states/{state_id}")

        assert result.status_code == 200
        body = result.get_json()
        assert body["state_id"] == state_id
        assert body["provenance"]["snapshot_id"] == response.network_snapshot_id
        assert body["kpis"]["business_network_cost"] == pytest.approx(1_200.0)

    def test_a_scenario_is_readable_by_snapshot_and_scenario(self, wired):
        client, _, response = wired
        result = client.get(
            f"/orchestrator/twin/snapshots/{response.network_snapshot_id}"
            f"?scenario_id={response.scenario_id}"
        )

        assert result.status_code == 200
        body = result.get_json()
        assert body["state_type"] == "SCENARIO"
        assert body["materialized_from_delta"] is True
        closed = [f["facility_id"] for f in body["facilities"] if not f["is_open"]]
        assert closed == ["DC_DELHI"]

    def test_flows_can_be_paged(self, wired):
        client, _, response = wired
        state_id = next(t["state_id"] for t in response.twin_states
                        if t["state_type"] == "OPTIMIZED")

        page = client.get(
            f"/orchestrator/twin/states/{state_id}?flow_offset=0&flow_limit=2"
        ).get_json()["flows"]

        assert len(page["items"]) == 2
        assert page["limit"] == 2
        assert page["total"] > 2

    def test_a_summary_view_omits_flows_but_keeps_the_aggregate(self, wired):
        client, _, response = wired
        state_id = response.twin_states[0]["state_id"]
        body = client.get(
            f"/orchestrator/twin/states/{state_id}?include_flows=false"
        ).get_json()

        assert body["flows"]["items"] == []
        assert body["flows"]["total"] > 0
        assert body["flow_aggregate"]["total_lanes"] > 0

    def test_a_malformed_paging_argument_falls_back_rather_than_erroring(self, wired):
        """A bad query string is a client mistake, not a server fault."""
        client, _, response = wired
        state_id = response.twin_states[0]["state_id"]
        result = client.get(
            f"/orchestrator/twin/states/{state_id}?flow_limit=banana"
        )
        assert result.status_code == 200

    def test_an_unknown_state_is_404(self, wired):
        client, _, _ = wired
        result = client.get("/orchestrator/twin/states/tws_nope")
        assert result.status_code == 404
        assert result.get_json()["error"]["code"] == "NOT_FOUND"

    def test_an_unknown_snapshot_is_404(self, wired):
        client, _, _ = wired
        assert client.get("/orchestrator/twin/snapshots/snap_nope").status_code == 404


class TestComparisonEndpoint:

    def test_a_scenario_compares_against_its_baseline(self, wired):
        client, _, response = wired
        result = client.get(
            "/orchestrator/twin/compare"
            f"?snapshot_id={response.network_snapshot_id}"
            f"&scenario_id={response.scenario_id}"
        )

        assert result.status_code == 200
        body = result.get_json()
        cost = next(d for d in body["kpi_deltas"]
                    if d["metric"] == "business_network_cost")
        assert cost["abs_delta"] == pytest.approx(400.0)
        assert cost["direction"] == "INCREASED"
        assert body["same_snapshot"] is True

    def test_two_states_compare_by_id(self, wired):
        client, _, response = wired
        baseline = next(t["state_id"] for t in response.twin_states
                        if t["state_type"] == "OPTIMIZED")
        scenario = next(t["state_id"] for t in response.twin_states
                        if t["state_type"] == "SCENARIO")

        body = client.get(
            f"/orchestrator/twin/compare?baseline={baseline}&comparison={scenario}"
        ).get_json()

        closed = [c["facility_id"] for c in body["facility_changes"]
                  if c["change"] == "CLOSED"]
        assert closed == ["DC_DELHI"]

    def test_missing_arguments_are_a_400(self, wired):
        client, _, _ = wired
        result = client.get("/orchestrator/twin/compare")
        assert result.status_code == 400
        assert result.get_json()["error"]["code"] == "INVALID_REQUEST"

    def test_comparing_an_unknown_state_is_404(self, wired):
        client, _, response = wired
        baseline = response.twin_states[0]["state_id"]
        result = client.get(
            f"/orchestrator/twin/compare?baseline={baseline}&comparison=tws_nope"
        )
        assert result.status_code == 404


class TestReasoningInsightsEndpoint:

    def test_network_insight_is_narrative_first_and_ui_ready(self, wired):
        client, _, response = wired
        state_id = next(t["state_id"] for t in response.twin_states
                        if t["state_type"] == "OPTIMIZED")

        result = client.post("/orchestrator/insights", json={
            "state_id": state_id,
            "scope": "NETWORK",
            "question": "What should a business leader notice?",
            "disable_llm": True,
        })

        assert result.status_code == 200
        reasoning = result.get_json()["reasoning"]
        assert reasoning["source"] == "template"
        assert reasoning["briefing"]["opening"].startswith("I ")
        assert reasoning["briefing"]["kpi_insights"]
        assert reasoning["grounding_status"] in ("GROUNDED", "NO_CLAIMS")

    def test_facility_and_lane_insights_validate_exact_map_entities(self, wired):
        client, _, response = wired
        state_id = next(t["state_id"] for t in response.twin_states
                        if t["state_type"] == "OPTIMIZED")

        facility = client.post("/orchestrator/insights", json={
            "state_id": state_id,
            "scope": "FACILITY",
            "entity_id": "DC_DELHI",
            "disable_llm": True,
        })
        lane = client.post("/orchestrator/insights", json={
            "state_id": state_id,
            "scope": "LANE",
            "entity_id": "DC_DELHI->MKT_NORTH",
            "disable_llm": True,
        })

        assert facility.status_code == 200
        assert facility.get_json()["reasoning"]["briefing"]["entity_id"] == "DC_DELHI"
        assert lane.status_code == 200
        assert lane.get_json()["reasoning"]["briefing"]["entity_id"] == \
            "DC_DELHI->MKT_NORTH"

    def test_unknown_map_entity_is_refused_without_guessing(self, wired):
        client, _, response = wired
        state_id = response.twin_states[0]["state_id"]

        result = client.post("/orchestrator/insights", json={
            "state_id": state_id,
            "scope": "FACILITY",
            "entity_id": "DC_JAIPUR",
            "disable_llm": True,
        })

        assert result.status_code == 400
        assert result.get_json()["error"]["code"] == "INVALID_ENTITY"


class TestRunResponseCarriesReferences:

    def test_the_run_endpoint_returns_twin_handles(self, wired):
        client, _, _ = wired
        result = client.post("/orchestrator/run", json={
            "input": "What is our current network state?",
            "disable_llm": True,
        })
        body = result.get_json()

        assert "twin_states" in body
        assert body["twin_states"]
        handle = body["twin_states"][0]
        assert handle["state_id"]
        assert handle["snapshot_id"]

        # And the handle resolves.
        follow_up = client.get(f"/orchestrator/twin/states/{handle['state_id']}")
        assert follow_up.status_code == 200
