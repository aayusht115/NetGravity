"""
The Action Agent as it is wired into this product, rather than in isolation.

The package arrived on a feature branch with 65 tests of its own, all of
which pass unmodified. What none of them could cover is the join: this
repository has TWO ingestion paths, and the one the completeness gate was
written against is not the one the browser uses.

    netgravity/ingestion/pipeline.py   the full pipeline — profiling, AI
                                       column mapping, a review console.
                                       `check_completeness` reads its
                                       `TabularResult.network_rows`.

    app/backend/api/ingestion_dynamic.py
                                       what the upload screen actually
                                       posts to. Calls
                                       `network_extractor.build_network_
                                       from_dataframes` and hands the
                                       result to `network_assembler`.

So every missing-data action item and every missing-data email would have
fired for exactly zero real uploads. `app/backend/services/
completeness_adapter.py` closes that: it is an ADAPTER, not a second
checker — it builds the two dictionaries `check_completeness` reads and
calls the one implementation, so the required/optional registries stay in
one place and adding a field there is seen by both paths.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.backend.api.network_extractor import build_network_from_dataframes
from app.backend.services.completeness_adapter import check_structure, rows_from_structure
from netgravity.action_agent.email_builder import (
    SUBJECT_LIMIT,
    _subject_line,
    build_investigate_email,
    build_recommendation_email,
)
from netgravity.ingestion.schemas.content import ContentType


def _workbook(drop_facility_columns=()):
    """A small, complete network — with columns removed on request."""
    facilities = pd.DataFrame([
        ("F001", "North Plant", "PLANT", "Toronto", "Ontario", 43.65, -79.38, 50000, 900000),
        ("F002", "Brampton DC", "DC", "Brampton", "Ontario", 43.68, -79.76, 30000, 450000),
        ("F003", "Calgary DC", "DC", "Calgary", "Alberta", 51.05, -114.07, 28000, 410000),
    ], columns=["Facility_ID", "Facility_Name", "Facility_Type", "City", "State",
                "Latitude", "Longitude", "Capacity_Units", "Fixed_Cost"])
    if drop_facility_columns:
        facilities = facilities.drop(columns=list(drop_facility_columns))

    markets = pd.DataFrame([
        ("M001", "Toronto Metro", "Toronto", "Ontario", 43.65, -79.38, 2),
        ("M002", "Calgary Metro", "Calgary", "Alberta", 51.05, -114.07, 2),
    ], columns=["Market_ID", "Market_Name", "City", "State", "Latitude",
                "Longitude", "Service_SLA_Days"])

    # Priced, because a lane without a rate is a REQUIRED gap of its own —
    # the registry asks for a transport cost on both legs — and this fixture
    # is meant to be complete except for what a test removes on purpose.
    lanes = pd.DataFrame([
        ("L001", "F001", "F002", "PLANT", "DC", 40.0, 1.0, 20000, True, 2.40),
        ("L002", "F002", "M001", "DC", "MARKET", 30.0, 1.0, 15000, True, 1.80),
        ("L003", "F003", "M002", "DC", "MARKET", 20.0, 1.0, 15000, True, 1.60),
    ], columns=["Lane_ID", "Origin_ID", "Destination_ID", "Origin_Type",
                "Destination_Type", "Distance_Miles", "Transit_Time_Days",
                "Capacity_Units", "Active", "Cost_Per_Unit"])

    demand = pd.DataFrame([
        ("2026-01", "M001", "P001", 4000, "RETAIL"),
        ("2026-01", "M002", "P001", 3000, "RETAIL"),
    ], columns=["Period", "Market_ID", "Product_ID", "Demand_Units", "Channel"])

    products = pd.DataFrame([("P001", "Widget", "CPG", 1.0, 10.0)],
                            columns=["Product_ID", "Product_Name",
                                     "Product_Category", "Unit_Weight_Kg", "Unit_Cost"])

    return {
        "f.xlsx::Facilities": facilities,
        "f.xlsx::Markets": markets,
        "f.xlsx::Lanes": lanes,
        "f.xlsx::Demand_History": demand,
        "f.xlsx::Products": products,
    }


class TestTheGateRunsOnThePathTheProductUses:
    def test_the_adapter_speaks_the_registry_s_key_names(self):
        """
        The extractor names its columns for the browser (`capacity`,
        `fixedCost`); the registry names them for the engine
        (`capacity_units_per_period`, `fixed_cost_per_year`). Nothing
        matches by string-munging — the correspondence is written out, so
        renaming either side breaks loudly here rather than silently
        reporting every field as missing.
        """
        view = rows_from_structure(build_network_from_dataframes(_workbook()))
        facilities = view.network_rows[ContentType.FACILITY]
        assert facilities, view.network_rows
        for row in facilities:
            assert "facility_name" in row, row
            assert "capacity_units_per_period" in row, row
            assert "role" in row, row

    def test_a_supply_site_and_a_dc_are_bucketed_by_the_extractor_s_own_decision(self):
        """
        The registry buckets a facility into "supply" or "dc" from its
        `role`, and the extractor has already made that call — it returns
        plants and DCs as two lists. Re-deriving it here would be a second
        classifier that could disagree with the first.
        """
        view = rows_from_structure(build_network_from_dataframes(_workbook()))
        roles = sorted(r["role"] for r in view.network_rows[ContentType.FACILITY])
        assert roles == ["DC", "DC", "PLANT"], roles

    def test_a_complete_workbook_reports_no_required_gap(self):
        report = check_structure(build_network_from_dataframes(_workbook()))
        assert report.missing_required == [], [
            m.as_dict() for m in report.missing_required]

    def test_a_removed_column_is_caught_and_named_against_each_site(self):
        """
        This is the case the whole feature exists for. Measured on the real
        Canadian workbook with Fixed_Cost dropped: fifteen gaps, one per
        distribution centre, each naming the site.
        """
        report = check_structure(
            build_network_from_dataframes(_workbook(drop_facility_columns=["Fixed_Cost"])))
        labels = {m.display_label for m in report.missing_required}
        assert labels == {"DC Annual Fixed Cost (per year)"}, labels
        names = sorted(m.entity_name for m in report.missing_required)
        assert names == ["Brampton DC", "Calgary DC"], names

    def test_a_plant_is_not_asked_for_a_dc_s_fixed_cost(self):
        """The registry scopes that field to the DC bucket; the adapter must
        not flatten the two."""
        report = check_structure(
            build_network_from_dataframes(_workbook(drop_facility_columns=["Fixed_Cost"])))
        assert "North Plant" not in {m.entity_name for m in report.missing_required}

    def test_the_labels_name_no_currency(self):
        """
        These strings are read verbatim by a client in an email. The
        registry hardcoded a rupee — "DC Annual Fixed Cost (₹ lakh/year)" —
        and this product infers the money unit from the upload, so a
        Canadian distributor was being asked for a figure in lakh/year.
        """
        report = check_structure(
            build_network_from_dataframes(_workbook(drop_facility_columns=["Fixed_Cost"])))
        for gap in report.missing_required + report.missing_optional:
            assert "₹" not in gap.display_label, gap.display_label
            assert "lakh" not in gap.display_label.lower(), gap.display_label

    def test_the_optional_registry_still_discriminates(self):
        """
        A gate that flags everything optional is not a gate. This workbook
        states an SLA and a demand history, so those two must NOT appear.
        """
        report = check_structure(build_network_from_dataframes(_workbook()))
        flagged = {m.canonical_key for m in report.missing_optional}
        assert "carbon_emission_factor" in flagged, flagged
        assert "sla_days" not in flagged, flagged


class TestNothingIsSentWithoutBeingAsked:
    def test_the_pipeline_gate_does_not_block_by_default(self):
        """
        The branch flipped `report.network_assembled` to False on any
        required gap, which would stop a dataset that finalizes today from
        finalizing. Reporting the gap and blocking on it are two decisions,
        and only the first one is safe to take on someone's behalf.
        """
        from netgravity.ingestion.config import IngestionConfig
        assert IngestionConfig().completeness_blocks_finalize is False

    def test_the_orchestrator_swallows_a_notification_failure(self):
        """
        A missed notification is recoverable; a governance decision lost to
        an SMTP timeout is not. The hook is lazily imported and every
        failure inside it is logged, not raised.
        """
        import inspect

        from netgravity.orchestrator.core.orchestrator import Orchestrator
        src = inspect.getsource(Orchestrator._notify_action_agent)
        assert "from netgravity.action_agent import triggers" in src, src
        assert "except Exception:" in src, src
        assert "logger.exception" in src, src
        assert "raise" not in src, src

    def test_the_ingestion_service_swallows_one_too(self):
        import inspect

        from netgravity.ingestion.service import IngestionService
        src = inspect.getsource(IngestionService._maybe_notify_completeness)
        assert "except Exception:" in src, src
        assert "logger.exception" in src, src

    def test_the_gate_cannot_break_an_upload(self):
        """
        This now runs on the critical path of every upload, and it is a
        reporting feature. A registry entry that trips over an unusual
        workbook must cost the user an action item, not their upload — the
        parse has already succeeded by the time it runs.
        """
        import re

        from pathlib import Path
        src = Path("app/backend/api/ingestion_dynamic.py").read_text(encoding="utf-8")
        block = src[src.index("check_structure(structure)") - 400:]
        block = block[:block.index("check_structure(structure)") + 300]
        assert "try:" in block, block
        assert re.search(r"except Exception:.*\n.*logger\.exception", block), block


class TestTheRecipientBookIsScopedToOneProject:
    def test_two_projects_do_not_share_an_address_book(self, tmp_path):
        """
        Measured before this: an address added while working on one network
        was offered, pre-ticked, on a project created afterwards by a
        DIFFERENT account. The store wrote one deployment-wide file.
        """
        from netgravity.action_agent.recipients import NotificationRecipientStore
        from netgravity.ingestion.storage.local import LocalStorage

        storage = LocalStorage(tmp_path)
        NotificationRecipientStore(storage, scope="pr-aaa").add("a@example.com")
        other = NotificationRecipientStore(storage, scope="pr-bbb").emails()
        assert other == [], other

    def test_the_unscoped_book_still_exists_for_the_pipeline(self, tmp_path):
        """
        A governance card fires with no project in hand, so the
        deployment-wide book has to stay — it is the default, not a
        leftover.
        """
        from netgravity.action_agent.recipients import (
            RECIPIENTS_KEY,
            NotificationRecipientStore,
        )
        from netgravity.ingestion.storage.local import LocalStorage

        storage = LocalStorage(tmp_path)
        store = NotificationRecipientStore(storage)
        assert store.key == RECIPIENTS_KEY
        store.add("ops@example.com")
        assert store.emails() == ["ops@example.com"]

    def test_a_scope_cannot_escape_its_own_key(self, tmp_path):
        from netgravity.action_agent.recipients import NotificationRecipientStore
        from netgravity.ingestion.storage.local import LocalStorage

        with pytest.raises(ValueError):
            NotificationRecipientStore(LocalStorage(tmp_path), scope="../../etc/passwd")


class TestASubjectLineIsALine:
    def test_a_long_headline_is_cut_at_a_word(self):
        """
        Measured against the live gateway. The Reasoning Agent's headline
        for "what happens if we close DC_EAST?" came back as its whole
        first paragraph — 305 characters — and all of it went into the
        subject after "[NetGravity] Please investigate:".
        """
        headline = ("I find that closing DC_EAST increases total business network "
                    "cost by 12,037.9 (8.0%) to 162,665.6 while still serving "
                    "100.0% of demand. My model shows four facilities remain open.")
        line = _subject_line(headline)
        assert len(line) <= SUBJECT_LIMIT + 1, (len(line), line)
        assert line.endswith("…"), line
        # Cut between words, never through one.
        assert headline.startswith(line[:-1]), line

    def test_a_short_headline_is_left_alone(self):
        assert _subject_line("Dallas DC is over capacity") == "Dallas DC is over capacity"

    def test_an_empty_headline_does_not_produce_a_bare_prefix(self):
        assert _subject_line("") == "NetGravity update"

    def test_both_card_emails_use_it(self):
        long_headline = "x" * 400
        for content in (
            build_recommendation_email(headline=long_headline, narrative="n", deep_link="/l"),
            build_investigate_email(headline=long_headline, narrative="n", deep_link="/l"),
        ):
            assert len(content.subject) < 120, content.subject
            # The full text is not lost — it opens the body.
            assert long_headline in content.body
