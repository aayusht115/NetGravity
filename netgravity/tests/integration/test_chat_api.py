"""
Phase 3 — the HTTP conversational surface.

Thin by design: the endpoint validates the body, delegates to `ChatService`, and
maps the outcome kind onto a status code. These tests check the wiring and the
status-code contract, not the conversational behaviour, which is covered by
`test_conversational_workflows.py`.
"""

from __future__ import annotations

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.api import create_orchestrator_blueprint

from .conftest import build_delhi_network

flask = pytest.importorskip("flask", reason="Flask is required for the HTTP surface")


@pytest.fixture
def client():
    from flask import Flask

    orchestrator = build_orchestrator(network=build_delhi_network(), enable_llm=False)
    app = Flask(__name__)
    app.register_blueprint(create_orchestrator_blueprint(orchestrator))
    app.config.update(TESTING=True)
    return app.test_client()


class TestChatEndpoint:

    def test_a_status_question_returns_200(self, client):
        response = client.post("/orchestrator/chat", json={
            "message": "How many warehouses do we have?", "disable_llm": True,
        })
        assert response.status_code == 200
        body = response.get_json()
        assert body["intent"] == "STATUS_QUERY"
        assert body["provenance"] == "OBSERVED"
        assert body["conversation_id"].startswith("conv_")

    def test_a_clarification_returns_202(self, client):
        """
        202, not 4xx: the exchange is unfinished, not malformed. A client that
        treated "please clarify" as an error would show the user a failure.
        """
        response = client.post("/orchestrator/chat", json={
            "message": "Close Delhi.", "disable_llm": True,
        })
        assert response.status_code == 202
        body = response.get_json()
        assert body["status"] == "AWAITING_CLARIFICATION"
        assert body["clarification"]["kind"] == "AMBIGUOUS_INTENT"

    def test_a_high_risk_finding_returns_202(self, client):
        response = client.post("/orchestrator/chat", json={
            "message": "There is a 70% probability of flooding around DC_DELHI.",
            "disable_llm": True,
        })
        assert response.status_code == 202
        body = response.get_json()
        assert body["status"] == "REQUIRES_HUMAN"
        assert body["risk"]["results"][0]["risk_factor"] == pytest.approx(0.94)

    def test_a_malformed_body_returns_400(self, client):
        assert client.post("/orchestrator/chat", json={}).status_code == 400
        assert client.post("/orchestrator/chat",
                           json={"message": "hi", "bogus": 1}).status_code == 400

    def test_conversation_history_is_retrievable(self, client):
        first = client.post("/orchestrator/chat", json={
            "message": "What is the risk exposure of DC_DELHI?", "disable_llm": True,
        }).get_json()
        cid = first["conversation_id"]

        client.post("/orchestrator/chat", json={
            "message": "Why?", "conversation_id": cid, "disable_llm": True,
        })

        history = client.get(f"/orchestrator/chat/{cid}/history")
        assert history.status_code == 200
        turns = history.get_json()["turns"]
        assert len(turns) == 2
        assert turns[0]["intent"] == "RESILIENCE_QUERY"

    def test_an_unknown_conversation_returns_404(self, client):
        assert client.get("/orchestrator/chat/conv_nope/history").status_code == 404

    def test_conversation_state_survives_across_requests(self, client):
        """The service is held on the blueprint, not rebuilt per request."""
        first = client.post("/orchestrator/chat", json={
            "message": "What is the risk exposure of DC_DELHI?", "disable_llm": True,
        }).get_json()
        second = client.post("/orchestrator/chat", json={
            "message": "Why?", "conversation_id": first["conversation_id"],
            "disable_llm": True,
        }).get_json()

        assert second["conversation_id"] == first["conversation_id"]
        assert second["resolved_entity_ids"] == ["DC_DELHI"]

    def test_the_response_carries_full_provenance(self, client):
        body = client.post("/orchestrator/chat", json={
            "message": "There is a 70% probability of flooding around DC_DELHI.",
            "disable_llm": True,
        }).get_json()

        assert body["execution_id"]
        assert body["network_snapshot_id"].startswith("snap_")
        assert body["intent_schema_version"]
        assert body["governance"]["classification"] == "HUMAN_ONLY"
        assert body["grounding_status"] in ("GROUNDED", "NO_CLAIMS")
