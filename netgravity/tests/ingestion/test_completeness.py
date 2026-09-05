"""
Tests for netgravity.ingestion.completeness.

Two claims under test:
  1. Required-field gaps are reported per named entity (Supply Location /
     Candidate DC / Demand Zone / Lane), using the exact display_label
     strings a planner would recognise — never the internal canonical_key.
  2. Optional-field gaps are reported dataset-wide, never blocking, and a
     field satisfied by contracts (rather than a tabular column) is
     correctly suppressed when has_contracts=True.
"""

from __future__ import annotations

from netgravity.ingestion.completeness import (
    ENTITY_DC,
    ENTITY_DEMAND_ZONE,
    ENTITY_LANE,
    ENTITY_SUPPLY,
    check_completeness,
)
from netgravity.ingestion.schemas.content import ContentType
from netgravity.ingestion.tabular import TabularResult


def _complete_outcome() -> TabularResult:
    outcome = TabularResult()
    outcome.network_rows[ContentType.FACILITY] = [
        {"facility_id": "SUP1", "facility_name": "Mumbai Plant", "role": "PLANT",
         "status": "EXISTING", "capacity_units_per_period": 5000},
        # A PROPOSED site: the only kind the MILP charges an opening cost for.
        {"facility_id": "DC1", "facility_name": "Pune DC", "role": "DC",
         "status": "CANDIDATE",
         "capacity_units_per_period": 8000, "fixed_cost_per_year": 1200000},
    ]
    outcome.network_rows[ContentType.MARKET] = [
        {"market_id": "MKT1", "market_name": "Pune Zone"},
    ]
    outcome.network_rows[ContentType.DEMAND] = [
        {"market_id": "MKT1", "quantity": 300},
    ]
    outcome.network_rows[ContentType.LANE] = [
        {"origin_id": "SUP1", "destination_id": "DC1", "rate_per_unit": 12},
        {"origin_id": "DC1", "destination_id": "MKT1", "rate_per_unit": 5},
    ]
    return outcome


def test_complete_dataset_has_no_missing_required():
    report = check_completeness(_complete_outcome())
    assert report.missing_required == []
    assert not report.is_blocking


def test_complete_dataset_reports_all_optional_fields_missing():
    report = check_completeness(_complete_outcome())
    labels = {m.display_label for m in report.missing_optional}
    assert "Carbon Emission Factor (kg CO₂/unit)" in labels
    assert "Service Level Target (%)" in labels
    assert "Contract Rate Card / Surcharge Details" in labels


def test_has_contracts_suppresses_contract_rate_card_gap():
    report = check_completeness(_complete_outcome(), has_contracts=True)
    labels = {m.display_label for m in report.missing_optional}
    assert "Contract Rate Card / Surcharge Details" not in labels


def test_missing_dc_fixed_cost_is_reported_against_the_named_dc():
    outcome = _complete_outcome()
    del outcome.network_rows[ContentType.FACILITY][1]["fixed_cost_per_year"]

    report = check_completeness(outcome)
    matches = [m for m in report.missing_required
              if m.canonical_key == "fixed_cost_per_year"]
    assert len(matches) == 1
    assert matches[0].entity_type == ENTITY_DC
    assert matches[0].entity_name == "Pune DC"
    assert matches[0].display_label == "DC Annual Fixed Cost (₹ lakh/year)"


def test_missing_supply_capacity_bucketed_as_supply_location():
    outcome = _complete_outcome()
    del outcome.network_rows[ContentType.FACILITY][0]["capacity_units_per_period"]

    report = check_completeness(outcome)
    matches = [m for m in report.missing_required
              if m.canonical_key == "capacity_units_per_period"]
    assert len(matches) == 1
    assert matches[0].entity_type == ENTITY_SUPPLY
    assert matches[0].entity_name == "Mumbai Plant"


def test_missing_demand_zone_name_is_reported():
    outcome = _complete_outcome()
    del outcome.network_rows[ContentType.MARKET][0]["market_name"]

    report = check_completeness(outcome)
    matches = [m for m in report.missing_required if m.display_label == "Demand Zone Name"]
    assert len(matches) == 1
    assert matches[0].entity_type == ENTITY_DEMAND_ZONE


def test_lane_legs_are_classified_source_to_dc_vs_dc_to_demand():
    outcome = _complete_outcome()
    del outcome.network_rows[ContentType.LANE][0]["rate_per_unit"]
    del outcome.network_rows[ContentType.LANE][1]["rate_per_unit"]

    report = check_completeness(outcome)
    labels = {m.display_label for m in report.missing_required if m.entity_type == ENTITY_LANE}
    assert "Transport Cost: Source → DC (₹/unit)" in labels
    assert "Transport Cost: DC → Demand Zone (₹/unit)" in labels


def test_unresolvable_lane_endpoints_are_skipped_not_misclassified():
    outcome = _complete_outcome()
    outcome.network_rows[ContentType.LANE].append(
        {"origin_id": "UNKNOWN1", "destination_id": "UNKNOWN2"})

    report = check_completeness(outcome)
    lane_gaps = [m for m in report.missing_required if m.entity_type == ENTITY_LANE]
    # Only the two resolvable lanes' legs — the unresolvable third lane
    # contributes nothing, since guessing its classification would be worse
    # than skipping it.
    assert len(lane_gaps) == 0


# ---------------------------------------------------------------------------
# Role-scoped optional fields
#
# The mechanism under test is OptionalFieldSpec.role_bucket, not opening cost.
# Opening cost is the first spec to use it; the last test here declares a
# throwaway spec of its own so the scoping is proven independently of it.
# ---------------------------------------------------------------------------

from netgravity.ingestion.completeness import (  # noqa: E402
    OPTIONAL_FIELDS,
    OptionalFieldSpec,
    _check_optional,
)

OPENING_COST_LABEL = "Candidate DC Opening Cost (₹ lakh)"


def test_candidate_dc_without_opening_cost_is_flagged_as_optional():
    report = check_completeness(_complete_outcome())

    matches = [m for m in report.missing_optional if m.canonical_key == "opening_cost"]
    assert len(matches) == 1
    assert matches[0].display_label == OPENING_COST_LABEL
    assert matches[0].entity_type == ENTITY_DC
    assert matches[0].entity_names == ["Pune DC"]
    # Optional means optional: a defaulted opening cost still solves.
    assert not report.is_blocking
    assert all(m.canonical_key != "opening_cost" for m in report.missing_required)


def test_supply_facility_without_opening_cost_is_not_flagged():
    outcome = _complete_outcome()
    # Only the supply plant remains — opening cost is meaningless for it.
    outcome.network_rows[ContentType.FACILITY] = [
        outcome.network_rows[ContentType.FACILITY][0]
    ]

    report = check_completeness(outcome)
    assert [m for m in report.missing_optional if m.canonical_key == "opening_cost"] == []


def test_opening_cost_present_on_every_candidate_is_not_flagged():
    outcome = _complete_outcome()
    for row in outcome.network_rows[ContentType.FACILITY]:
        if row["role"] == "DC":
            row["opening_cost"] = 450

    report = check_completeness(outcome)
    assert [m for m in report.missing_optional if m.canonical_key == "opening_cost"] == []


def test_only_the_candidates_actually_missing_it_are_named():
    outcome = _complete_outcome()
    facilities = outcome.network_rows[ContentType.FACILITY]
    facilities.append({"facility_id": "DC2", "facility_name": "Nagpur DC", "role": "DC",
                       "status": "CANDIDATE",
                       "capacity_units_per_period": 6000, "fixed_cost_per_year": 900000,
                       "opening_cost": 380})
    facilities.append({"facility_id": "DC3", "facility_name": "Ludhiana DC", "role": "DC",
                       "status": "CANDIDATE",
                       "capacity_units_per_period": 4000, "fixed_cost_per_year": 700000})

    report = check_completeness(outcome)
    matches = [m for m in report.missing_optional if m.canonical_key == "opening_cost"]
    assert len(matches) == 1
    # Nagpur has it and is left out; the other two are named, in row order.
    assert matches[0].entity_names == ["Pune DC", "Ludhiana DC"]
    # entity_name stays filled for consumers that only read the single field.
    assert matches[0].entity_name == "Pune DC, Ludhiana DC"


def test_a_blank_opening_cost_counts_as_absent():
    outcome = _complete_outcome()
    outcome.network_rows[ContentType.FACILITY][1]["opening_cost"] = "   "

    report = check_completeness(outcome)
    matches = [m for m in report.missing_optional if m.canonical_key == "opening_cost"]
    assert [m.entity_names for m in matches] == [["Pune DC"]]


def test_role_bucket_scoping_works_for_any_optional_spec():
    """Generality: the scoping is a property of the spec, not of opening cost."""
    spec = OptionalFieldSpec("gate_hours", "Inbound Gate Hours", "hours/day",
                             "would let us model receiving windows",
                             ContentType.FACILITY, role_bucket="supply")
    assert spec not in OPTIONAL_FIELDS  # never registered; declared here only

    outcome = _complete_outcome()
    missing = []
    _check_optional_for(spec, outcome, missing)

    assert len(missing) == 1
    assert missing[0].entity_type == ENTITY_SUPPLY
    assert missing[0].entity_names == ["Mumbai Plant"]   # the DC is not named

    # And satisfied once the supply row carries it.
    outcome.network_rows[ContentType.FACILITY][0]["gate_hours"] = 16
    missing = []
    _check_optional_for(spec, outcome, missing)
    assert missing == []


def test_an_unscoped_optional_spec_still_checks_the_whole_dataset():
    """The dataset-wide path is unchanged for specs that declare no bucket."""
    spec = OptionalFieldSpec("dock_count", "Dock Count", "docks",
                             "would let us model dock contention",
                             ContentType.FACILITY)

    outcome = _complete_outcome()
    missing = []
    _check_optional_for(spec, outcome, missing)
    assert len(missing) == 1
    assert missing[0].entity_names == []   # dataset-wide gaps name no entity

    # One row carrying it anywhere satisfies an unscoped spec.
    outcome.network_rows[ContentType.FACILITY][0]["dock_count"] = 4
    missing = []
    _check_optional_for(spec, outcome, missing)
    assert missing == []


def _check_optional_for(spec, outcome, missing):
    """Run _check_optional against exactly one spec, registry untouched."""
    import netgravity.ingestion.completeness as mod

    original = mod.OPTIONAL_FIELDS
    mod.OPTIONAL_FIELDS = [spec]
    try:
        _check_optional(outcome, False, missing)
    finally:
        mod.OPTIONAL_FIELDS = original


# ---------------------------------------------------------------------------
# Status scoping
#
# Opening cost is charged by the MILP only for `fac.is_candidate`
# (milp.py opening_cost_term). A warehouse the client already operates will
# never incur one, so reporting it as missing sends a planner hunting for a
# number that does not exist.
# ---------------------------------------------------------------------------


def test_an_existing_warehouse_is_not_asked_for_an_opening_cost():
    outcome = _complete_outcome()
    outcome.network_rows[ContentType.FACILITY].append(
        {"facility_id": "DC9", "facility_name": "Chennai DC (operating)", "role": "DC",
         "status": "EXISTING", "capacity_units_per_period": 5000,
         "fixed_cost_per_year": 800000})

    report = check_completeness(outcome)
    matches = [m for m in report.missing_optional if m.canonical_key == "opening_cost"]
    assert len(matches) == 1
    assert matches[0].entity_names == ["Pune DC"], (
        "an operating warehouse was reported as missing a build cost it will "
        "never incur")


def test_a_closed_site_is_not_asked_for_one_either():
    outcome = _complete_outcome()
    outcome.network_rows[ContentType.FACILITY][1]["status"] = "CLOSED"

    report = check_completeness(outcome)
    assert [m for m in report.missing_optional if m.canonical_key == "opening_cost"] == []


def test_the_status_synonyms_the_assembler_accepts_are_accepted_here():
    """
    One table, shared with network_assembler.py. A gate that disagreed with
    the assembler would flag what the model does not think is wrong.
    """
    for stated in ("CANDIDATE", "PLANNED", "PROPOSED", "proposed"):
        outcome = _complete_outcome()
        outcome.network_rows[ContentType.FACILITY][1]["status"] = stated
        report = check_completeness(outcome)
        assert [m for m in report.missing_optional
                if m.canonical_key == "opening_cost"], stated

    for stated in ("EXISTING", "ACTIVE", "OPEN", "OPERATIONAL"):
        outcome = _complete_outcome()
        outcome.network_rows[ContentType.FACILITY][1]["status"] = stated
        report = check_completeness(outcome)
        assert not [m for m in report.missing_optional
                    if m.canonical_key == "opening_cost"], stated


def test_an_unstated_status_reports_the_status_not_the_opening_cost():
    """
    With no status column nothing can tell an operating site from a proposed
    one, and the two assemblers in this codebase default it in OPPOSITE
    directions. The honest gap is the status column itself.
    """
    outcome = _complete_outcome()
    for row in outcome.network_rows[ContentType.FACILITY]:
        row.pop("status", None)

    report = check_completeness(outcome)
    assert [m for m in report.missing_optional if m.canonical_key == "opening_cost"] == []
    status_gap = [m for m in report.missing_optional if m.canonical_key == "status"]
    assert len(status_gap) == 1
    assert status_gap[0].display_label == "Facility Status (existing or proposed)"


def test_an_unrecognised_status_is_treated_as_unstated():
    outcome = _complete_outcome()
    outcome.network_rows[ContentType.FACILITY][1]["status"] = "MOTHBALLED"

    report = check_completeness(outcome)
    assert [m for m in report.missing_optional if m.canonical_key == "opening_cost"] == []


def test_status_scoping_works_for_any_spec_that_declares_it():
    """Generality: the scoping is a property of the spec, not of opening cost."""
    spec = OptionalFieldSpec("decommission_cost", "Decommissioning Cost", "₹ lakh",
                             "would let us price closing a site",
                             ContentType.FACILITY, facility_status="EXISTING")

    outcome = _complete_outcome()
    missing = []
    _check_optional_for(spec, outcome, missing)

    # Only the EXISTING plant is in scope; the CANDIDATE DC is invisible to it.
    assert len(missing) == 1
    assert missing[0].entity_names == ["Mumbai Plant"]


def test_a_mixed_status_file_names_the_row_that_is_blank():
    """
    The regression. One facility stating a status used to satisfy the check
    for the whole file — so a blank row was reported nowhere, AND was
    invisible to the status-scoped checks, falling through everything.
    """
    outcome = _complete_outcome()
    facilities = outcome.network_rows[ContentType.FACILITY]
    facilities.append({"facility_id": "DC9", "facility_name": "Mumbai DC", "role": "DC",
                       "capacity_units_per_period": 6000, "fixed_cost_per_year": 90})

    report = check_completeness(outcome)
    status_gap = [m for m in report.missing_optional if m.canonical_key == "status"]
    assert len(status_gap) == 1
    assert status_gap[0].entity_names == ["Mumbai DC"], (
        "the blank row is not named, so nobody is told which site is unclear")
    assert status_gap[0].entity_type == "Facility"


def test_a_fully_stated_file_reports_no_status_gap():
    report = check_completeness(_complete_outcome())
    assert [m for m in report.missing_optional if m.canonical_key == "status"] == []


def test_a_blank_status_row_is_still_kept_out_of_the_opening_cost_check():
    """
    Being named in the status gap is the answer for an unclear row — guessing
    it is a candidate and asking for a build cost is not.
    """
    outcome = _complete_outcome()
    outcome.network_rows[ContentType.FACILITY].append(
        {"facility_id": "DC9", "facility_name": "Mumbai DC", "role": "DC",
         "capacity_units_per_period": 6000, "fixed_cost_per_year": 90})

    report = check_completeness(outcome)
    opening = [m for m in report.missing_optional if m.canonical_key == "opening_cost"]
    assert opening and "Mumbai DC" not in opening[0].entity_names
