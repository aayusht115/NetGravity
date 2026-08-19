"""
NetGravity V1.1 — Infeasibility Diagnostics Tests
===================================================
Tests that the diagnostic module correctly identifies structural causes
of infeasibility BEFORE the solver runs.
"""

from __future__ import annotations

import pytest

from netgravity.diagnostics.infeasibility import diagnose_infeasibility
from netgravity.schemas.network import (
    CanonicalNetwork,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
    ProductRecord,
    TransportMode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**kwargs) -> OptimizationConfig:
    return OptimizationConfig(**kwargs)


def _plant(pid: str, cap: float = 1000.0) -> FacilityRecord:
    return FacilityRecord(
        id=pid, name=f"Plant {pid}", role=NodeRole.PLANT,
        status=FacilityStatus.EXISTING,
        capacity_units_per_period=cap,
        is_mandatory=True,
    )


def _dc(did: str, cap: float = 1000.0) -> FacilityRecord:
    return FacilityRecord(
        id=did, name=f"DC {did}", role=NodeRole.DC,
        status=FacilityStatus.EXISTING,
        capacity_units_per_period=cap,
    )


def _market(mid: str) -> FacilityRecord:
    return FacilityRecord(id=mid, name=f"Market {mid}", role=NodeRole.MARKET)


def _demand(mid: str, pid: str, q: float, sla: float = None) -> DemandRecord:
    return DemandRecord(
        market_id=mid, product_id=pid, quantity=q, sla_days=sla
    )


def _product(pid: str = "P1") -> ProductRecord:
    return ProductRecord(id=pid, name="Widget", weight_kg=1.0)


def _lane(o: str, d: str, lt: float = 1.0, rate: float = 1.0) -> LaneRecord:
    return LaneRecord(
        origin_id=o, destination_id=d,
        mode=TransportMode.ROAD, rate_per_unit=rate,
        distance_km=100.0, lead_time_days=lt,
    )


# ---------------------------------------------------------------------------
# Test: No arcs to market
# ---------------------------------------------------------------------------

class TestNoArcsDetection:

    def test_detects_market_with_no_arcs(self):
        """Diagnostic should flag markets with no inbound lanes."""
        dc = _dc("DC")
        mkt_served  = _market("MKT_OK")
        mkt_stranded = _market("MKT_STRANDED")

        net = CanonicalNetwork(
            facilities=[dc, mkt_served, mkt_stranded],
            products=[_product()],
            demands=[
                _demand("MKT_OK",       "P1", 100.0),
                _demand("MKT_STRANDED", "P1",  50.0),
            ],
            lanes=[_lane("DC", "MKT_OK")],   # no lane to MKT_STRANDED
            config=_cfg(),
        )

        diag = diagnose_infeasibility(net)
        assert diag.has_issues
        assert "MKT_STRANDED" in diag.markets_with_no_arcs
        assert "MKT_OK" not in diag.markets_with_no_arcs

    def test_no_issues_when_all_markets_reachable(self):
        """No diagnostic issues when every market has at least one arc."""
        dc = _dc("DC")
        mkt1 = _market("M1")
        mkt2 = _market("M2")

        net = CanonicalNetwork(
            facilities=[dc, mkt1, mkt2],
            products=[_product()],
            demands=[_demand("M1", "P1", 100.0), _demand("M2", "P1", 50.0)],
            lanes=[_lane("DC", "M1"), _lane("DC", "M2")],
            config=_cfg(),
        )
        diag = diagnose_infeasibility(net)
        assert "M1" not in diag.markets_with_no_arcs
        assert "M2" not in diag.markets_with_no_arcs


# ---------------------------------------------------------------------------
# Test: SLA-blocked markets
# ---------------------------------------------------------------------------

class TestSLABlockedDetection:

    def test_detects_sla_blocked_market(self):
        """Market with tight SLA and only slow lanes should be detected."""
        dc = _dc("DC")
        mkt = _market("MKT")

        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product()],
            demands=[_demand("MKT", "P1", 100.0, sla=1.0)],  # SLA = 1 day
            lanes=[_lane("DC", "MKT", lt=5.0)],               # lead time = 5 days
            config=_cfg(enforce_sla=True),
        )
        diag = diagnose_infeasibility(net)
        assert diag.has_issues
        assert "MKT" in diag.markets_with_no_sla_arcs

    def test_sla_blocking_disabled_when_enforce_sla_false(self):
        """When enforce_sla=False, SLA-blocked markets should not be flagged."""
        dc = _dc("DC")
        mkt = _market("MKT")

        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product()],
            demands=[_demand("MKT", "P1", 100.0, sla=1.0)],
            lanes=[_lane("DC", "MKT", lt=5.0)],
            config=_cfg(enforce_sla=False),   # SLA not enforced
        )
        diag = diagnose_infeasibility(net)
        assert "MKT" not in diag.markets_with_no_sla_arcs


# ---------------------------------------------------------------------------
# Test: Capacity shortfall
# ---------------------------------------------------------------------------

class TestCapacityShortfall:

    def test_detects_total_capacity_shortfall(self):
        """When total capacity < total demand, diagnostic flags a shortfall."""
        dc = _dc("DC", cap=50.0)   # capacity = 50
        mkt = _market("MKT")

        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product()],
            demands=[_demand("MKT", "P1", 200.0)],   # demand = 200 > cap
            lanes=[_lane("DC", "MKT")],
            config=_cfg(allow_shortage=False),
        )
        diag = diagnose_infeasibility(net)
        assert diag.capacity_gap < 0
        assert diag.has_issues
        assert diag.total_demand == 200.0
        assert diag.total_capacity == 50.0

    def test_no_shortfall_when_capacity_sufficient(self):
        """When total capacity >= total demand, no shortfall flagged."""
        dc = _dc("DC", cap=500.0)  # more than enough
        mkt = _market("MKT")

        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product()],
            demands=[_demand("MKT", "P1", 100.0)],
            lanes=[_lane("DC", "MKT")],
            config=_cfg(),
        )
        diag = diagnose_infeasibility(net)
        assert diag.capacity_gap >= 0
        assert diag.total_demand == 100.0


# ---------------------------------------------------------------------------
# Test: Forced-close cascade
# ---------------------------------------------------------------------------

class TestForcedCloseCascade:

    def test_detects_market_blocked_by_forced_close(self):
        """
        Market served ONLY by forced-closed facility should be flagged.
        """
        dc_closed = FacilityRecord(
            id="DC_CLOSED", name="Closed DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=1000,
            is_forced_closed=True,  # <- forced closed
        )
        mkt = _market("MKT")

        net = CanonicalNetwork(
            facilities=[dc_closed, mkt],
            products=[_product()],
            demands=[_demand("MKT", "P1", 100.0)],
            lanes=[_lane("DC_CLOSED", "MKT")],  # only route is through closed DC
            config=_cfg(),
        )
        diag = diagnose_infeasibility(net)
        assert diag.has_issues
        assert "MKT" in diag.markets_blocked_by_forced_close

    def test_no_cascade_when_alternative_exists(self):
        """If another open facility also serves the market, no cascade issue."""
        dc_closed = FacilityRecord(
            id="DC_CLOSED", name="Closed DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=1000,
            is_forced_closed=True,
        )
        dc_open = _dc("DC_OPEN", cap=1000.0)
        mkt = _market("MKT")

        net = CanonicalNetwork(
            facilities=[dc_closed, dc_open, mkt],
            products=[_product()],
            demands=[_demand("MKT", "P1", 100.0)],
            lanes=[
                _lane("DC_CLOSED", "MKT"),
                _lane("DC_OPEN",   "MKT"),   # alternative exists
            ],
            config=_cfg(),
        )
        diag = diagnose_infeasibility(net)
        # MKT should NOT be in forced-close cascade (has alternative)
        assert "MKT" not in diag.markets_blocked_by_forced_close


# ---------------------------------------------------------------------------
# Test: Diagnostic print report (smoke test)
# ---------------------------------------------------------------------------

class TestDiagnosticReport:

    def test_print_report_no_errors(self):
        """print_report() should run without errors."""
        dc = _dc("DC")
        mkt = _market("MKT_NO_ARC")

        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product()],
            demands=[_demand("MKT_NO_ARC", "P1", 100.0)],
            lanes=[],  # no lanes
            config=_cfg(),
        )
        diag = diagnose_infeasibility(net)
        # Should not raise
        diag.print_report()

    def test_healthy_network_no_issues(self):
        """Healthy network: diagnostic has no issues."""
        dc = _dc("DC")
        mkt = _market("MKT")

        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product()],
            demands=[_demand("MKT", "P1", 100.0)],
            lanes=[_lane("DC", "MKT")],
            config=_cfg(),
        )
        diag = diagnose_infeasibility(net)
        assert not diag.has_issues
        assert not diag.markets_with_no_arcs
        assert not diag.markets_with_no_sla_arcs
        assert not diag.markets_blocked_by_forced_close
        assert diag.capacity_gap >= 0

    def test_summary_populated(self):
        """Summary narrative should be populated for any diagnostic."""
        dc = _dc("DC")
        mkt = _market("MKT_NO_ARC")

        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product()],
            demands=[_demand("MKT_NO_ARC", "P1", 100.0)],
            lanes=[],
            config=_cfg(),
        )
        diag = diagnose_infeasibility(net)
        assert len(diag.summary) > 0, "Diagnostic summary should not be empty"


# ---------------------------------------------------------------------------
# Test: Echelon capacity shortfall regression test
# ---------------------------------------------------------------------------

class TestEchelonCapacityShortfall:

    def test_detects_echelon_capacity_shortfall_when_upstream_abundant(self):
        """
        DC capacity < demand while plant capacity is abundant must produce
        a non-empty, correct diagnosis (not 'no obvious cause').
        """
        plant = _plant("P1", cap=10000.0)  # Plant capacity abundant (10,000)
        dc = _dc("DC1", cap=500.0)         # DC capacity bottleneck (500)
        mkt = _market("M1")

        net = CanonicalNetwork(
            facilities=[plant, dc, mkt],
            products=[_product()],
            demands=[_demand("M1", "P1", 1000.0)],  # Demand 1,000 > DC capacity 500
            lanes=[_lane("P1", "DC1"), _lane("DC1", "M1")],
            config=_cfg(allow_shortage=False),
        )

        diag = diagnose_infeasibility(net)
        assert diag.has_issues, "Diagnostic must flag issues when DC capacity is below demand"
        assert diag.capacity_gap < 0, "Capacity gap must be negative"
        assert diag.total_capacity == 500.0, "Total effective capacity must be the bottleneck DC capacity (500.0)"
        assert any("DC" in s or "capacity" in s for s in diag.summary)
        assert not any("No obvious" in s for s in diag.summary), "Summary must NOT say 'No obvious structural causes detected'"

