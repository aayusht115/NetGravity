from __future__ import annotations

import io

from flask import Flask

from netgravity.ingestion.api import create_ingestion_blueprint


def test_upload_api_returns_ui_ready_links_and_review(tmp_config):
    app = Flask(__name__)
    app.register_blueprint(create_ingestion_blueprint(tmp_config))
    client = app.test_client()

    csv = (
        b"Facility_ID,Facility_Name,Type,Dock Door Count\n"
        b"DC_1,Delhi DC,DC,8\n"
    )
    response = client.post(
        "/api/ingestions",
        data={
            "client_id": "client_a",
            "files": (io.BytesIO(csv), "warehouse master.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["status"] == "AWAITING_REVIEW"
    assert payload["links"]["draft"].endswith("/draft")
    assert payload["review"]["has_blocking"] is True
    assert "source" not in payload

    run_id = payload["run_id"]
    reviews = client.get(f"/api/ingestions/{run_id}/reviews").get_json()
    unfamiliar = [item for item in reviews["review"]["items"]
                  if item["kind"] == "unfamiliar_field"]
    assert unfamiliar
    assert unfamiliar[0]["blocking"] is False
    assert unfamiliar[0]["context"]["raw_preserved"] is True
    assert unfamiliar[0]["ui"]["section"] == "unfamiliar_fields"
    assert "ask_ai" in unfamiliar[0]["ui"]["actions"]

    analysed = client.post(
        f"/api/ingestions/{run_id}/reviews/analyse",
        json={"item_id": unfamiliar[0]["item_id"],
              "user_text": "What could this field mean?"},
    )
    assert analysed.status_code == 200
    analysed_payload = analysed.get_json()
    assert analysed_payload["requires_confirmation"] is True
    suggestion = analysed_payload["suggestion"]
    assert len(suggestion["display"].split()) <= 35


def test_api_rejects_stale_revision_before_mutating(tmp_config):
    app = Flask(__name__)
    blueprint = create_ingestion_blueprint(tmp_config)
    app.register_blueprint(blueprint)
    client = app.test_client()
    response = client.post(
        "/api/ingestions",
        data={"files": (io.BytesIO(
            b"Facility_ID,Facility_Name,Type\nDC_1,Delhi,DC\n"), "sites.csv")},
        content_type="multipart/form-data",
    )
    payload = response.get_json()
    item = next(i for i in payload["review"]["items"] if i["blocking"])

    stale = client.post(
        f"/api/ingestions/{payload['run_id']}/reviews",
        json={
            "revision": payload["revision"] + 1,
            "decisions": [{"item_id": item["item_id"],
                           "value": item["proposed_value"]}],
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "REVISION_CONFLICT"
