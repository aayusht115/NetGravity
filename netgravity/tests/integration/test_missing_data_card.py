"""
The Missing-Data Opportunity — what the upload does not contain, said before
the user confirms it.

Three kinds of check.

  * Contract: the parse response carries a completeness report, judged by the
    SAME registry the session pipeline uses. The preview path ran no
    completeness check at all before this, so an upload with no candidate-DC
    opening cost reached the solver with it defaulted to zero and nothing on
    screen said so.

  * Action: "Request this data" RAISES A REQUEST the orchestrator owns
    (data_requests.py), which then hands off to the Action Agent. The route
    never sends anything itself — a dispatcher must not be the record, or a
    failed send loses the fact that a planner asked. No credential is
    configured, so every send is stubbed and nothing leaves the machine.

  * Generality: the card is a mechanism, not a warning about one field. The
    rendering module is read here to prove it names no field at all, and the
    report is exercised with a gap that has nothing to do with opening cost.
"""

from __future__ import annotations

import io
import json
import pathlib
import re
import shutil
import subprocess
import uuid

import pytest

from app.backend.app import app

PASSWORD = "Netgravity@2026"
FRONTEND = pathlib.Path(app.root_path).parent / "frontend"
CARD_JS = FRONTEND / "js" / "missing-data-card.js"

#: Cleared for the same reason as netgravity/tests/action_agent/conftest.py:
#: a developer's real .env must never turn a test into a live send.
_EMAIL_ENV_VARS = (
    "NETGRAVITY_SMTP_HOST", "NETGRAVITY_SMTP_PORT", "NETGRAVITY_SMTP_USERNAME",
    "NETGRAVITY_SMTP_PASSWORD", "NETGRAVITY_SMTP_USE_TLS", "NETGRAVITY_SMTP_FROM_ADDRESS",
    "NETGRAVITY_EMAIL_API_KEY", "NETGRAVITY_EMAIL_STRICT",
    "NETGRAVITY_DEFAULT_RECIPIENT_EMAIL", "NETGRAVITY_DEFAULT_TEST_RECIPIENT_EMAIL",
    "NETGRAVITY_INBOUND_EMAIL_DOMAIN", "NETGRAVITY_APP_BASE_URL",
)


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Send the Action Agent's stores at a throwaway root, in stub mode."""
    for name in _EMAIL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NETGRAVITY_DATA_ROOT", str(tmp_path))
    for zone in ("raw", "standardized", "curated"):
        (tmp_path / zone).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def auth(client):
    email = f"gap-{uuid.uuid4().hex[:10]}@example.com"
    res = client.post("/api/auth/signup", json={
        "name": "Missing Data", "email": email, "password": PASSWORD})
    assert res.status_code == 201, res.get_data(as_text=True)[:300]
    token = res.get_json()["token"]
    res = client.post("/api/projects", json={"name": "Missing data"},
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code in (200, 201), res.get_data(as_text=True)[:300]
    body = res.get_json()
    return token, body.get("id") or body.get("project", {}).get("id")


def _workbook(opening_costs=None) -> bytes:
    """
    Two proposed DCs, one operating DC and a plant. `opening_costs` states the
    column's values; None means the cell is blank, and omitting the argument
    entirely means the column is not in the file at all.
    """
    pd = pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    facilities = {
        "Facility_ID": ["PLT1", "DC1", "DC2", "DC3"],
        "Facility_Name": ["Mumbai Plant", "Bengaluru South", "Nagpur",
                          "Chennai DC"],
        "Facility_Type": ["PLANT", "DC", "DC", "DC"],
        # Two PROPOSED sites and one the client already operates. The
        # operating one must never be asked for a build cost.
        "Status": ["EXISTING", "CANDIDATE", "CANDIDATE", "EXISTING"],
        "Capacity_Units": [9000, 1000, 2000, 3000],
        "Fixed_Cost": [80.0, 50.0, 60.0, 70.0],
        "Latitude": [19.07, 12.97, 21.15, 13.08],
        "Longitude": [72.87, 77.59, 79.09, 80.27],
    }
    if opening_costs is not None:
        facilities["Opening_Cost"] = opening_costs

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(facilities).to_excel(writer, sheet_name="Facilities", index=False)
        pd.DataFrame({
            "Market_ID": ["M1", "M2"],
            "Market_Name": ["Pune Zone", "Indore Zone"],
            "Demand_Units": [300, 250],
            "Latitude": [18.52, 22.72],
            "Longitude": [73.86, 75.86],
        }).to_excel(writer, sheet_name="Markets", index=False)
    return buf.getvalue()


def _parse(client, auth, workbook=None):
    token, project_id = auth
    res = client.post(
        "/api/ingestions/preview/upload-and-parse",
        data={
            "files": (io.BytesIO(workbook if workbook is not None else _workbook()),
                      "network.xlsx"),
            "project_id": project_id,
        },
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.get_data(as_text=True)[:400]
    return res.get_json()


def _visible_text(html: str) -> str:
    """What a reader actually sees: tags, and so attributes, removed."""
    return re.sub(r"<[^>]*>", " ", html)


def _gap(report, canonical_key, tier="missing_optional"):
    return next((g for g in report[tier] if g["canonical_key"] == canonical_key), None)


class TestTheParseCarriesTheReport:

    def test_the_preview_response_includes_a_completeness_report(self, client, auth):
        report = _parse(client, auth)["completeness"]
        assert "missing_required" in report and "missing_optional" in report

    def test_candidate_dcs_with_no_opening_cost_are_named(self, client, auth):
        gap = _gap(_parse(client, auth)["completeness"], "opening_cost")
        assert gap is not None, "an absent opening-cost column was not reported"
        assert gap["entity_names"] == ["Bengaluru South", "Nagpur"]
        assert gap["entity_type"] == "Candidate DC"
        assert gap["display_label"] == "Candidate DC Opening Cost (₹ lakh)"
        assert gap["what_it_unlocks"]

    def test_the_supply_plant_is_not_named(self, client, auth):
        gap = _gap(_parse(client, auth)["completeness"], "opening_cost")
        assert "Mumbai Plant" not in gap["entity_names"], (
            "opening cost is meaningless for an existing supply plant")

    def test_an_operating_warehouse_is_not_named(self, client, auth):
        """
        The MILP charges opening cost only for `fac.is_candidate`. A warehouse
        the client already runs will never incur one, and reporting it sends a
        planner hunting for a number that does not exist.
        """
        gap = _gap(_parse(client, auth)["completeness"], "opening_cost")
        assert "Chennai DC" not in gap["entity_names"]

    def test_a_file_with_no_status_column_is_asked_for_the_status_instead(
            self, client, auth):
        pd = pytest.importorskip("pandas")
        pytest.importorskip("openpyxl")
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            pd.DataFrame({
                "Facility_ID": ["DC1", "DC2"],
                "Facility_Name": ["Bengaluru South", "Nagpur"],
                "Facility_Type": ["DC", "DC"],
                "Capacity_Units": [1000, 2000],
                "Fixed_Cost": [50.0, 60.0],
                "Latitude": [12.97, 21.15],
                "Longitude": [77.59, 79.09],
            }).to_excel(writer, sheet_name="Facilities", index=False)
            pd.DataFrame({
                "Market_ID": ["M1"], "Market_Name": ["Pune Zone"],
                "Demand_Units": [300], "Latitude": [18.52], "Longitude": [73.86],
            }).to_excel(writer, sheet_name="Markets", index=False)

        report = _parse(client, auth, buf.getvalue())["completeness"]
        assert _gap(report, "opening_cost") is None, (
            "with no status stated, nothing can tell an operating site from a "
            "proposed one — the honest gap is the status column")
        assert _gap(report, "status") is not None

    def test_only_the_candidates_actually_missing_it_are_named(self, client, auth):
        data = _parse(client, auth, _workbook(opening_costs=[None, None, 420, None]))
        gap = _gap(data["completeness"], "opening_cost")
        assert gap is not None
        assert gap["entity_names"] == ["Bengaluru South"]

    def test_a_complete_column_is_not_reported_at_all(self, client, auth):
        data = _parse(client, auth, _workbook(opening_costs=[None, 380, 420, None]))
        assert _gap(data["completeness"], "opening_cost") is None

    def test_other_absent_fields_are_reported_by_the_same_mechanism(self, client, auth):
        """Generality: opening cost is one entry in a registry, not the feature."""
        report = _parse(client, auth)["completeness"]
        labels = {g["display_label"] for g in report["missing_optional"]}
        assert "Carbon Emission Factor (kg CO₂/unit)" in labels
        assert "Historical Monthly Demand (last 12 months)" in labels


class TestRequestingTheData:

    def test_with_no_registered_contact_the_request_still_stands(
            self, client, auth, isolated_stores):
        """
        NO_CONTACT is a state of a real request, not a failure of one. The
        planner asked; there is simply nobody registered to ask yet, and that
        must not discard what they asked for.
        """
        token, project_id = auth
        _parse(client, auth)

        res = client.post("/api/ingestions/preview/request-missing-data",
                          json={"project_id": project_id, "kind": "optional"},
                          headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "NO_CONTACT"
        assert body["request_id"].startswith("dreq_")
        assert body["field_labels"], "the request records what was asked for"
        assert body["note"]

    def test_the_orchestrator_records_the_request_then_hands_off(
            self, client, auth, isolated_stores):
        from netgravity.action_agent.dispatch_log import DispatchLogStore
        from netgravity.ingestion.config import IngestionConfig
        from netgravity.ingestion.storage import get_storage

        token, project_id = auth
        _parse(client, auth)

        res = client.post("/api/ingestions/preview/source-contact",
                          json={"project_id": project_id, "email": "owner@acme.com",
                                "name": "Data Owner"},
                          headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200, res.get_data(as_text=True)[:300]

        res = client.post("/api/ingestions/preview/request-missing-data",
                          json={"project_id": project_id, "kind": "optional"},
                          headers={"Authorization": f"Bearer {token}"})
        body = res.get_json()
        assert body["status"] == "NOTIFIED", body
        assert body["recipient"] == "owner@acme.com"
        assert "Candidate DC Opening Cost (₹ lakh)" in body["field_labels"]

        # The record exists independently of the notification.
        from netgravity.orchestrator.data_requests import DataRequestStore

        storage = get_storage(IngestionConfig())
        stored = DataRequestStore(storage).get(body["request_id"])
        assert stored is not None
        assert stored.subject_id == project_id
        assert stored.tier == "optional"
        assert stored.fields, "the request remembers what was asked for"

        # The notification is the downstream effect, keyed to the request.
        records = DispatchLogStore(storage).list_all()
        assert len(records) == 1
        assert records[0].trigger_type == "optional_data"
        assert records[0].reference_id == body["request_id"]
        # Stubbed, never sent: no credential is configured.
        assert records[0].result == "stubbed"

    def test_the_route_never_calls_the_action_agent_itself(self):
        """
        The handoff is the point. A route that dispatches directly makes the
        dispatcher the record, and a failed send then loses the request.
        """
        import pathlib

        source = (pathlib.Path(app.root_path) / "api"
                  / "ingestion_dynamic.py").read_text(encoding="utf-8")
        assert "action_agent import triggers" not in source
        assert "on_completeness_failure" not in source
        assert "orchestrator.request_missing_data(" in source

    def test_a_second_request_is_deduped(self, client, auth, isolated_stores):
        from netgravity.action_agent.dispatch_log import DispatchLogStore
        from netgravity.ingestion.config import IngestionConfig
        from netgravity.ingestion.storage import get_storage

        token, project_id = auth
        _parse(client, auth)
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/ingestions/preview/source-contact",
                    json={"project_id": project_id, "email": "owner@acme.com"},
                    headers=headers)

        first = client.post("/api/ingestions/preview/request-missing-data",
                            json={"project_id": project_id, "kind": "optional"},
                            headers=headers)
        second = client.post("/api/ingestions/preview/request-missing-data",
                             json={"project_id": project_id, "kind": "optional"},
                             headers=headers)

        # Asking twice is asking once: the standing request comes back
        # unchanged and nothing is sent again.
        assert second.get_json()["request_id"] == first.get_json()["request_id"]
        assert second.get_json()["status"] == "NOTIFIED"
        assert len(DispatchLogStore(get_storage(IngestionConfig())).list_all()) == 1

    def test_a_tier_with_no_gaps_is_refused(self, client, auth, isolated_stores):
        token, project_id = auth
        data = _parse(client, auth)
        assert data["completeness"]["missing_required"] == [], (
            "fixture assumption: this workbook has no required gaps")

        res = client.post("/api/ingestions/preview/request-missing-data",
                          json={"project_id": project_id, "kind": "required"},
                          headers={"Authorization": f"Bearer {token}"})
        assert res.status_code >= 400


class TestTheCardIsAMechanismNotAWarning:
    """
    The card must render whatever the report contains. The surest way for it
    to stop doing that is for a field name to appear in it.
    """

    #: Every canonical key in either registry, plus the labels' distinguishing
    #: words. None of these may be written into the rendering module.
    FORBIDDEN = [
        "opening_cost", "openingCost", "carbon_emission_factor", "sla_days",
        "service_level", "fuel_surcharge_pct", "lane_capacity",
        "fixed_cost_per_year", "capacity_units_per_period",
    ]

    def test_the_card_module_names_no_field(self):
        source = CARD_JS.read_text(encoding="utf-8")
        # Comments explain the rule by naming what it bans; scanning them
        # would teach the next reader to delete the explanation.
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        source = re.sub(r"^\s*//.*$", "", source, flags=re.M)
        found = [name for name in self.FORBIDDEN if name in source]
        assert not found, (
            f"the missing-data card hardcodes {found}; it must render whatever "
            f"the completeness report contains")

    def test_the_card_never_shows_an_internal_engine_key(self):
        """
        `canonical_key` may be used to group and to key the DOM, never to
        build a sentence — a planner would not recognise it.
        """
        source = CARD_JS.read_text(encoding="utf-8")
        for line in source.splitlines():
            if "canonical_key" not in line and "canonicalKey" not in line:
                continue
            assert not re.search(r"(headline|consequence|facts)\s*\(", line), line

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_it_renders_a_field_that_is_not_opening_cost(self, tmp_path):
        """
        The generality case, rendered for real: a report containing only a
        lead-time gap must produce a complete card, with the label, the
        consequence and the entity names all taken from the report.
        """
        report = {
            "missing_required": [],
            "missing_optional": [{
                "canonical_key": "sla_days",
                "display_label": "Maximum Delivery Lead Time (days)",
                "unit": "days",
                "entity_type": "Demand Zone",
                "entity_name": "Pune Zone, Indore Zone",
                "entity_names": ["Pune Zone", "Indore Zone"],
                "what_it_unlocks": "would let us flag lanes that miss your delivery window",
            }],
        }
        script = tmp_path / "render.mjs"
        script.write_text(
            f"import {{ missingDataCardsHtml }} from {json.dumps(str(CARD_JS))};\n"
            f"process.stdout.write(missingDataCardsHtml({json.dumps(report)}, {{}}));\n",
            encoding="utf-8")
        html = subprocess.run(["node", str(script)], capture_output=True,
                              text=True, check=True).stdout

        assert "2 demand zones are missing their Maximum Delivery Lead Time (days)." in html
        assert "would let us flag lanes that miss your delivery window" in html
        assert "Pune Zone, Indore Zone" in html
        assert "optional — results still run" in html
        assert "Request this data" in html
        # The internal key may key the DOM; it may never be read by a person.
        assert "sla_days" not in _visible_text(html)

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_a_required_gap_reads_as_blocking(self, tmp_path):
        report = {
            "missing_required": [
                {"canonical_key": "rate_per_unit",
                 "display_label": "Transport Cost: DC → Demand Zone (₹/unit)",
                 "unit": "₹/unit", "entity_type": "Lane",
                 "entity_name": "Pune DC → Pune Zone",
                 "entity_names": ["Pune DC → Pune Zone"], "what_it_unlocks": ""},
                {"canonical_key": "rate_per_unit",
                 "display_label": "Transport Cost: DC → Demand Zone (₹/unit)",
                 "unit": "₹/unit", "entity_type": "Lane",
                 "entity_name": "Pune DC → Indore Zone",
                 "entity_names": ["Pune DC → Indore Zone"], "what_it_unlocks": ""},
            ],
            "missing_optional": [],
        }
        script = tmp_path / "render_required.mjs"
        script.write_text(
            f"import {{ missingDataCardsHtml }} from {json.dumps(str(CARD_JS))};\n"
            f"process.stdout.write(missingDataCardsHtml({json.dumps(report)}, {{}}));\n",
            encoding="utf-8")
        html = subprocess.run(["node", str(script)], capture_output=True,
                              text=True, check=True).stdout

        # Two rows of the SAME field collapse into one statement about both.
        assert "2 lanes are missing their Transport Cost" in html
        assert "The analysis cannot run until this is provided." in html
        assert "required — results are blocked" in html
        assert html.count("ing-gap-row") == 1


class TestMissingDataTravelsWithTheResults:
    """
    The gaps and the results belong to one analysis, so they arrive together.
    A screen showing "here is what your network costs" and "here is what would
    make that number better" should not have to ask twice.
    """

    def test_the_completeness_report_survives_the_commit(self):
        """
        The preview is dropped on commit, and that is exactly when the gaps
        start mattering — the analysis has now run on this data.
        """
        import pathlib

        source = pathlib.Path(
            "app/backend/services/dataset_store.py").read_text(encoding="utf-8")
        assert 'record_commit' in source
        assert '"completeness": preview.get("completeness", {})' in source, (
            "the gaps are dropped with the preview, so Optimized Results has "
            "nothing to report")

    def test_the_insights_response_carries_the_gaps(self):
        import pathlib

        from app.backend.app import app

        source = (pathlib.Path(app.root_path) / "api"
                  / "insights.py").read_text(encoding="utf-8")
        assert '"missing_data": _missing_data_for(project_id)' in source
        assert "def _missing_data_for" in source

    def test_the_gaps_are_read_not_recomputed(self):
        import pathlib

        from app.backend.app import app

        source = (pathlib.Path(app.root_path) / "api"
                  / "insights.py").read_text(encoding="utf-8")
        block = source.split("def _missing_data_for")[1].split("\n    def ")[0]
        assert "dataset_store.committed" in block
        assert "check_completeness" not in block, (
            "the report was measured at review time; recomputing it here "
            "could disagree with what the reviewer was shown")

    def test_requesting_the_data_costs_no_model_call(self):
        """
        Clicking "Request this data" passes a structured request to the
        orchestrator. The wording was produced with the results; the click
        needs no new reasoning.
        """
        import pathlib

        from app.backend.app import app

        source = (pathlib.Path(app.root_path) / "api"
                  / "ingestion_dynamic.py").read_text(encoding="utf-8")
        block = source.split("def request_preview_missing_data")[1]
        block = block.split("\n@")[0]
        assert "orchestrator.request_missing_data(" in block
        for reasoning in ("ReasoningAgent", "reason(", "ExplanationService"):
            assert reasoning not in block, (
                f"the request path invokes reasoning ({reasoning!r}); the "
                f"wording already exists and the click must not re-ask")
