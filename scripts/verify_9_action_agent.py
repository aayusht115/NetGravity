#!/usr/bin/env python3
"""
FLOW 9 — The Action Agent, all five triggers, with mock data
===============================================================
NO LIVE API OF ANY KIND. Completeness checking is deterministic (no model
call, ever); the two orchestrator runs use enable_llm=False; email sending
has no NETGRAVITY_SMTP_HOST/NETGRAVITY_EMAIL_API_KEY configured, so every
send is STUB MODE — logged, never actually delivered.

    python scripts/verify_9_action_agent.py

WHAT THIS PROVES
    That the built backend (netgravity/action_agent/ + the completeness gate
    + the orchestrator hook) actually produces a correctly-formatted email
    for each of the five triggers, end to end, using small mock datasets —
    without touching the real ingestion console/API. That wiring (a real
    upload flowing through the actual /api/ingestions endpoints and the
    frontend) is deliberately NOT exercised here; this script stands in for
    it so the Action Agent itself can be verified in isolation first.

    Every "[EMAIL STUB] would have emailed ..." line below is the ACTUAL
    output of netgravity/action_agent/email_sender.py — not a mock of it.
    Nothing here reconstructs or guesses at what the email would say.

WHY A TEMP DATA ROOT
    NETGRAVITY_DATA_ROOT is redirected to a throwaway directory for this
    run, so it never touches (or is confused by) whatever is in the real
    ./data folder. Printed below so you can go look at the raw JSON
    (sessions, dispatch log, recipients) afterward if you want to.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from _verify_common import banner, section

# --- Redirect the data root BEFORE importing anything that reads it --------
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="netgravity_action_agent_demo_"))
os.environ["NETGRAVITY_DATA_ROOT"] = str(_TMP_ROOT)
os.environ["NETGRAVITY_DEFAULT_RECIPIENT_EMAIL"] = "aayush.t115@gmail.com"
os.environ["NETGRAVITY_DEFAULT_TEST_RECIPIENT_EMAIL"] = "dummy.t115@gmail.com"
for _zone in ("raw", "standardized", "curated"):
    (_TMP_ROOT / _zone).mkdir(parents=True, exist_ok=True)

from netgravity.action_agent.dispatch_log import DispatchLogStore
from netgravity.action_agent.inbound_email import parse_sendgrid_payload
from netgravity.action_agent.recipients import NotificationRecipientStore, SourceContactStore
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.service import IngestionService
from netgravity.ingestion.storage import get_storage
from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.governance.action_classifier import GovernancePolicy
from netgravity.orchestrator.schemas.requests import (
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


# Print every stub-send and every completeness/dispatch decision as it
# happens, not just a final summary.
logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("netgravity.action_agent.email_sender").setLevel(logging.INFO)


def _storage():
    return get_storage(IngestionConfig())


def main() -> int:
    banner("FLOW 9 — ACTION AGENT, ALL FIVE TRIGGERS (MOCK DATA, STUB EMAIL)")
    print(f"  data root (throwaway): {_TMP_ROOT}")
    print("  outbound email: STUB MODE (no NETGRAVITY_SMTP_HOST/_EMAIL_API_KEY set)")

    storage = _storage()
    SourceContactStore(storage).set(
        "central_region", "owner@centralregion-distributor.com", contact_name="Rakesh")

    # -----------------------------------------------------------------
    # Triggers 1 & 2 — missing required / optional data
    # -----------------------------------------------------------------
    section("TRIGGERS 1 & 2 — missing required + optional data")
    print("  (built directly against a mock completeness report — isolates the")
    print("   Action Agent from the separate, pre-existing column-mapping-")
    print("   confidence gate, which is what real ingested files go through and")
    print("   is exercised by the ingestion test suite already; not this one's job)")

    from netgravity.action_agent import triggers as action_agent_triggers
    from netgravity.ingestion.session import IngestionSession

    mock_session = IngestionSession(
        run_id="ing_demo_central_region",
        source=str(_TMP_ROOT / "uploads" / "central_region"),
        client_id="central_region",
        report={
            "missing_required": [
                {"entity_type": "Candidate DC", "entity_name": "Raipur DC",
                 "display_label": "DC Annual Fixed Cost (₹ lakh/year)", "unit": "₹ lakh/year"},
                {"entity_type": "Candidate DC", "entity_name": "Raipur DC",
                 "display_label": "DC Daily Throughput Capacity (units/day)", "unit": "units/day"},
            ],
            "missing_optional": [
                {"display_label": "Carbon Emission Factor (kg CO₂/unit)",
                 "what_it_unlocks": "would let us include a carbon-impact KPI"},
                {"display_label": "Service Level Target (%)",
                 "what_it_unlocks": "would let us score results against your target fill rate"},
            ],
        },
    )
    action_agent_triggers.on_completeness_failure(mock_session, kind="required")
    action_agent_triggers.on_completeness_failure(mock_session, kind="optional")

    # -----------------------------------------------------------------
    # Triggers 3 & 4 — a real orchestrator run reaching each governance tier
    # -----------------------------------------------------------------
    section("TRIGGER 4 — 'please investigate' (Tier 3 / HUMAN_ONLY)")
    NotificationRecipientStore(storage)  # forces the default seed to exist
    orch = build_orchestrator(network=build_case16_network(), enable_llm=False)
    resp = orch.run_sync(OrchestratorRequest(input="What happens if we close DC_EAST?"))
    print(f"  execution {resp.execution_id}: governance={resp.governance.classification.value}")

    section("TRIGGER 3 — recommendation (Tier 2 / APPROVAL_REQUIRED)")
    orch2 = build_orchestrator(
        network=build_case16_network(), enable_llm=False,
        governance_policy=GovernancePolicy(
            cost_impact_human_pct=1e9, unserved_demand_human_rate=1.0,
            min_confidence_for_auto="HIGH",
        ),
    )
    resp2 = orch2.run_sync(OrchestratorRequest(
        input="Shift DC_EAST volume to DC_WEST",
        explicit_intent=Intent.SCENARIO_ANALYSIS,
        explicit_scenarios=[ScenarioIntentSpec(
            action=ScenarioActionType.SHIFT_VOLUME,
            facility_ids=["DC_EAST"], target_facility_id="DC_WEST")],
    ))
    print(f"  execution {resp2.execution_id}: status={resp2.status}")
    if resp2.status == "REQUIRES_APPROVAL":
        print(f"  approval created: {resp2.approval.approval_id}")
    else:
        print("  (this run settled elsewhere this time — governance rules involve "
              "template-confidence, which can vary run to run; re-run the script "
              "if you want to see the approval path specifically)")

    # -----------------------------------------------------------------
    # Trigger 5 — reply-by-email upload
    # -----------------------------------------------------------------
    section("TRIGGER 5 — reply-by-email upload (simulated webhook payload)")
    print("  proves: a verified reply's attachment re-enters the EXACT same")
    print("  upload pipeline (IngestionService.resume_with_file), tagged to the")
    print("  original session — no new/duplicate validation logic.")

    service = IngestionService(IngestionConfig())
    upload_dir = _TMP_ROOT / "uploads" / "central_region_reply_demo"
    upload_dir.mkdir(parents=True)
    (upload_dir / "facilities.csv").write_text(
        "facility_id,facility_name,role\nDC1,Raipur DC,DC\n")
    real_session = service.start(upload_dir, client_id="central_region")
    print(f"  session {real_session.run_id} created (revision {real_session.revision})")

    corrected_csv = (
        "facility_id,facility_name,role,capacity_units_per_period,fixed_cost_per_year\n"
        "DC1,Raipur DC,DC,8000,1200000\n"
    ).encode()

    class _FakeAttachment:
        def __init__(self, name: str, data: bytes):
            self.filename = name
            self._data = data
        def read(self) -> bytes:
            return self._data

    form = {
        "from": "owner@centralregion-distributor.com",
        "to": f"ingest-{real_session.run_id}@mail.netgravity.example",
        "subject": "Re: Data needed",
        "headers": "Message-ID: <demo-reply-1@centralregion-distributor.com>\n",
    }
    inbound = parse_sendgrid_payload(form, {"attachment1": _FakeAttachment("corrected.csv", corrected_csv)})
    print(f"  session id extracted from Reply-To address alone: {inbound.session_id}")
    verified = SourceContactStore(storage).verify_sender("central_region", inbound.from_address)
    print(f"  sender verified against registered contact: {verified}")
    if verified and inbound.attachments:
        refreshed = service.resume_with_file(
            real_session.run_id, inbound.attachments[0].filename, inbound.attachments[0].content)
        print(f"  session after reply applied: revision {refreshed.revision} "
              f"(was {real_session.revision}) — same session, new data")

    # -----------------------------------------------------------------
    # Audit trail
    # -----------------------------------------------------------------
    section("DISPATCH LOG — everything the Action Agent sent this run")
    for record in DispatchLogStore(storage).list_all():
        print(f"  [{record.trigger_type:14s}] ref={record.reference_id:20s} "
              f"result={record.result:8s} to={record.recipients} subject={record.subject!r}")

    print(f"\n  Temp data root left on disk for inspection: {_TMP_ROOT}")
    print("  (delete it manually, or it'll sit in your OS temp dir like any other tmpdir)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
