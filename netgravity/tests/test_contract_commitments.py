"""
Contractual site commitments: read from a document, enforced by the solver.

The MILP has constrained this since V1.4. Constraint C5c pins `y_i = 1` for a
facility whose `contract_status` is ACTIVE and which does not permit early
closure; validation check V-015 names the conflict when a scenario closes one
anyway; the Digital Twin reports it and `metrics/contracts.py` summarises it.

And nothing had ever set the fields. No ingestion path, no API, no scenario
override — `contract_status` defaulted to NONE on every facility of every
network, so the enforcement was structurally present and permanently inert. A
planner could be shown a recommendation to close a site the client was
contractually unable to close, and the one part of the system whose job was to
object had no way to know.

These tests cover the whole chain: clause -> `FacilityCommitment` ->
`FacilityRecord` -> MILP -> validation.
"""

from __future__ import annotations

import pytest

from netgravity.ingestion.contracts_to_network import (
    apply_commitments,
    apply_contract_rules,
    commitments_from_rules,
)
from netgravity.ingestion.schemas.contract import (
    ContractRule,
    FacilityCommitment,
)
from netgravity.optimization.milp import milp_solve
from netgravity.schemas.network import ContractStatus
from netgravity.tests.fixtures.case16_synthetic import build_case16_network
from netgravity.validation.checks import validate_network


@pytest.fixture(scope="module")
def network():
    return build_case16_network()


@pytest.fixture(scope="module")
def closed_dc(network):
    """A DC the unconstrained solve chooses to close — the interesting case."""
    result = milp_solve(network, None)
    closed = [d.facility_id for d in result.facility_decisions
              if not d.is_open and str(d.role) in {"DC", "NodeRole.DC"}]
    assert closed, "the fixture must close at least one DC for this to test anything"
    return closed[0]


# ===========================================================================

class TestClausesReachTheFacilityRecord:

    def test_an_active_lock_in_sets_the_fields_the_milp_reads(self, network):
        target = [f for f in network.facilities if f.role.value == "DC"][0]
        assert target.contract_status is ContractStatus.NONE, \
            "the pre-condition of this whole area: nothing sets it"

        result = apply_commitments(network, [FacilityCommitment(
            facility_id=target.id, is_active=True, allows_early_closure=False,
            early_exit_penalty=250_000.0,
            source_excerpt="Lessee shall not terminate prior to expiry.")])

        bound = next(f for f in result.network.facilities if f.id == target.id)
        assert bound.contract_status is ContractStatus.ACTIVE
        assert bound.contract_allows_early_closure is False
        assert bound.closure_cost == pytest.approx(250_000.0)
        assert bound.contract_prohibits_closure is True
        assert target.id in result.pinned_open
        assert target.id in result.priced_exit

    def test_an_expired_term_is_recorded_as_expired(self, network):
        """
        A contract that has ended is a fact worth recording: it is what makes a
        closure permissible.
        """
        target = [f for f in network.facilities if f.role.value == "DC"][0]
        result = apply_commitments(network, [FacilityCommitment(
            facility_id=target.id, is_active=False,
            source_excerpt="The term expired on 2025-03-31.")])
        bound = next(f for f in result.network.facilities if f.id == target.id)
        assert bound.contract_status is ContractStatus.EXPIRED
        assert bound.contract_prohibits_closure is False

    def test_the_network_gets_a_new_data_version(self, network):
        """
        A pinned facility changes the feasible set and an exit penalty changes
        the objective, so this is a different network. Sharing the observed
        version would let a snapshot store hand back the un-stamped one.
        """
        target = [f for f in network.facilities if f.role.value == "DC"][0]
        result = apply_commitments(network, [FacilityCommitment(
            facility_id=target.id, is_active=True, allows_early_closure=False)])
        assert result.network.data_version != network.data_version

    def test_a_facility_name_matches_when_no_id_was_extracted(self, network):
        target = [f for f in network.facilities if f.role.value == "DC"][0]
        result = apply_commitments(network, [FacilityCommitment(
            facility_label=target.name, is_active=True,
            allows_early_closure=False)])
        assert target.id in result.applied


class TestSilenceIsNeverReadAsALockIn:
    """
    The rule that decides whether this feature helps or harms. An unstated term
    must not pin a facility open: that would block a closure the client is free
    to make, on the strength of a clause nobody wrote.
    """

    def test_an_unstated_early_closure_term_does_not_prohibit_closure(self):
        commitment = FacilityCommitment(
            facility_id="DC_X", is_active=True, allows_early_closure=None)
        assert commitment.prohibits_closure is False

    def test_an_end_date_alone_is_not_a_lock_in(self, network):
        """A term that ends is not a term that cannot be exited."""
        target = [f for f in network.facilities if f.role.value == "DC"][0]
        result = apply_commitments(network, [FacilityCommitment(
            facility_id=target.id, term_end_date="2027-01-01")])
        bound = next(f for f in result.network.facilities if f.id == target.id)
        assert bound.contract_prohibits_closure is False
        assert target.id not in result.pinned_open

    def test_no_penalty_is_invented_from_a_term_and_a_rent(self, network):
        target = [f for f in network.facilities if f.role.value == "DC"][0]
        before = target.closure_cost
        result = apply_commitments(network, [FacilityCommitment(
            facility_id=target.id, is_active=True, allows_early_closure=False,
            term_end_date="2028-06-30", notice_period_days=90)])
        bound = next(f for f in result.network.facilities if f.id == target.id)
        assert bound.closure_cost == pytest.approx(before), \
            "a penalty nobody stated must not appear"

    def test_a_tri_state_parse_keeps_unstated_distinct_from_false(self):
        from netgravity.ingestion.ai.contract_reader import _to_commitments

        parsed = _to_commitments([
            {"facility_id": "A", "allows_early_closure": False,
             "facility_label": "A"},
            {"facility_id": "B", "allows_early_closure": None,
             "facility_label": "B"},
            {"facility_id": "C", "facility_label": "C"},
        ])
        assert [c.allows_early_closure for c in parsed] == [False, None, None]
        assert not any(c.prohibits_closure for c in parsed), \
            "none of these states an active term, so none prohibits closure"


class TestUnmatchedCommitmentsAreReportedNotForced:

    def test_a_commitment_for_a_site_this_network_lacks_is_reported(self, network):
        result = apply_commitments(network, [FacilityCommitment(
            facility_label="Warehouse Nobody Has", is_active=True,
            allows_early_closure=False)])
        assert result.applied == {}
        assert "Warehouse Nobody Has" in result.unmatched
        assert any("does not contain" in w for w in result.warnings)
        assert result.network is network, "an unmatched commitment changes nothing"

    def test_nothing_is_fuzzy_matched(self, network):
        """
        The cost of binding the wrong building is a plan that cannot be
        executed, so a near miss is a report rather than a guess.
        """
        target = [f for f in network.facilities if f.role.value == "DC"][0]
        near_miss = (target.name or "")[:-3] + "XYZ"
        result = apply_commitments(network, [FacilityCommitment(
            facility_label=near_miss, is_active=True,
            allows_early_closure=False)])
        assert result.applied == {}
        assert near_miss in result.unmatched


class TestTheSolverActuallyHonoursIt:

    def test_a_pinned_facility_stays_open_and_costs_more(self, network, closed_dc):
        """
        The end of the chain. The solve without a contract closes this DC; with
        the contract applied it must stay open, and the objective must rise —
        holding a site open the model wanted to close is not free.
        """
        without = milp_solve(network, None)
        applied = apply_commitments(network, [FacilityCommitment(
            facility_id=closed_dc, is_active=True, allows_early_closure=False,
            source_excerpt="Minimum term clause 4.2.")])
        with_contract = milp_solve(applied.network, None)

        was_open = {d.facility_id: d.is_open for d in without.facility_decisions}
        now_open = {d.facility_id: d.is_open for d in with_contract.facility_decisions}
        assert was_open[closed_dc] is False
        assert now_open[closed_dc] is True, "C5c must pin it open"
        assert (with_contract.solver.objective_value
                > without.solver.objective_value), \
            "holding a site open against the optimum must cost something"

    def test_a_scenario_closing_it_is_refused_not_costed(self, network, closed_dc):
        """
        The failure mode this exists to produce: a scenario that violates a
        contract is reported INFEASIBLE with a named check, rather than costed
        as though it were a decision the client could take.
        """
        applied = apply_commitments(network, [FacilityCommitment(
            facility_id=closed_dc, is_active=True, allows_early_closure=False)])
        forced = applied.network.model_copy(update={"facilities": [
            f.model_copy(update={"is_forced_closed": True})
            if f.id == closed_dc else f
            for f in applied.network.facilities]})

        report = validate_network(forced)
        assert "V-015" in [i.code for i in report.errors], \
            "the conflict must be named, not merely infeasible"

        result = milp_solve(forced, None)
        assert result.solver.status.value == "INFEASIBLE"

    def test_a_stated_exit_penalty_is_charged_once_when_the_model_closes(self):
        """
        An early-exit penalty becomes `closure_cost`, which the objective
        already charges on an open → closed transition.

        Built as its own two-DC network rather than taken from the fixture,
        because a closure cost applies only to an EXISTING facility that the
        model closes — an unselected CANDIDATE is not a closure, and picking a
        candidate out of the fixture tested the wrong thing.
        """
        from netgravity.schemas.network import (
            CanonicalNetwork, DemandRecord, FacilityRecord, FacilityStatus,
            LaneRecord, NodeRole, OptimizationConfig, ProductRecord,
            TransportMode,
        )

        def build(penalty: float) -> CanonicalNetwork:
            return CanonicalNetwork(
                network_id="EXIT_PENALTY",
                facilities=[
                    FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                                   status=FacilityStatus.EXISTING,
                                   capacity_units_per_period=9999,
                                   is_mandatory=True, is_closable=False),
                    # Expensive and unnecessary: the model closes it unless a
                    # penalty makes closing worse than keeping it.
                    FacilityRecord(id="DC_SPARE", name="Spare DC", role=NodeRole.DC,
                                   status=FacilityStatus.EXISTING,
                                   capacity_units_per_period=500,
                                   fixed_cost_per_year=1200.0),
                    FacilityRecord(id="DC_MAIN", name="Main DC", role=NodeRole.DC,
                                   status=FacilityStatus.EXISTING,
                                   capacity_units_per_period=500),
                    FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET,
                                   status=FacilityStatus.EXISTING, is_closable=False),
                ],
                products=[ProductRecord(id="P1", name="P1", weight_kg=1.0)],
                demands=[DemandRecord(market_id="MKT", product_id="P1",
                                      quantity=100.0)],
                lanes=[
                    LaneRecord(origin_id="PLANT", destination_id="DC_SPARE",
                               mode=TransportMode.ROAD, rate_per_unit=1.0,
                               distance_km=10.0, lead_time_days=1.0),
                    LaneRecord(origin_id="PLANT", destination_id="DC_MAIN",
                               mode=TransportMode.ROAD, rate_per_unit=1.0,
                               distance_km=10.0, lead_time_days=1.0),
                    LaneRecord(origin_id="DC_SPARE", destination_id="MKT",
                               mode=TransportMode.ROAD, rate_per_unit=2.0,
                               distance_km=10.0, lead_time_days=1.0),
                    LaneRecord(origin_id="DC_MAIN", destination_id="MKT",
                               mode=TransportMode.ROAD, rate_per_unit=2.0,
                               distance_km=10.0, lead_time_days=1.0),
                ],
                config=OptimizationConfig(
                    enable_inventory=False, enforce_sla=False,
                    enable_carbon_cost=False, allow_shortage=True,
                    verbose=False, enable_closure_cost=True),
            )

        # Without a penalty the model closes the spare DC.
        plain = milp_solve(build(0.0), None)
        spare = next(d for d in plain.facility_decisions
                     if d.facility_id == "DC_SPARE")
        assert spare.is_open is False, "the spare DC must be closable for this test"

        # With a penalty applied from a contract clause, it is CHARGED.
        applied = apply_commitments(build(0.0), [FacilityCommitment(
            facility_id="DC_SPARE", is_active=True,
            allows_early_closure=True,       # closure permitted, but priced
            early_exit_penalty=400.0)])
        bound = next(f for f in applied.network.facilities
                     if f.id == "DC_SPARE")
        assert bound.closure_cost == pytest.approx(400.0)

        priced = milp_solve(applied.network, None)
        decision = next(d for d in priced.facility_decisions
                        if d.facility_id == "DC_SPARE")
        if not decision.is_open:
            assert priced.objective_components["closure_cost"] == pytest.approx(400.0)
            assert decision.closure_cost == pytest.approx(400.0)
        else:
            # The penalty made keeping it cheaper than closing it, which is the
            # other correct outcome and the reason the penalty matters.
            assert priced.objective_components["closure_cost"] == pytest.approx(0.0)


class TestExtractedContractsFlowThrough:

    def test_commitments_are_collected_from_contract_rules(self):
        rules = [
            ContractRule(contract_id="C1", vendor_name="V", base_rate=10.0,
                         facility_commitments=[
                             FacilityCommitment(facility_id="A", facility_label="A",
                                                is_active=True)]),
            ContractRule(contract_id="C2", vendor_name="W", base_rate=12.0),
        ]
        assert len(commitments_from_rules(rules)) == 1

    def test_apply_contract_rules_stamps_the_network(self, network):
        target = [f for f in network.facilities if f.role.value == "DC"][0]
        rules = [ContractRule(
            contract_id="C1", vendor_name="V", base_rate=10.0,
            facility_commitments=[FacilityCommitment(
                facility_id=target.id, is_active=True,
                allows_early_closure=False)])]
        result = apply_contract_rules(network, rules)
        assert target.id in result.applied

    def test_a_rate_card_with_no_commitments_changes_nothing(self, network):
        """The common case for a freight contract, and it must be free."""
        rules = [ContractRule(contract_id="C1", vendor_name="V", base_rate=10.0)]
        result = apply_contract_rules(network, rules)
        assert result.applied == {}
        assert result.network is network
        assert result.warnings == []
