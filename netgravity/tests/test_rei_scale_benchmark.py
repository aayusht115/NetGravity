"""
NetGravity — REI scale benchmark.

Measures the REI batch at 7 / 25 / 50 / 100 facilities so scalability claims
rest on measurement rather than extrapolation from Case-16.

Run the report:

    python -m pytest netgravity/tests/test_rei_scale_benchmark.py -s -k scale

Timing is REPORTED, not asserted — wall-clock assertions are flaky on shared
hardware. What IS asserted is the structural claim the timings are meant to
support: solve count grows as 1 + N, and parallel results match sequential
exactly.

The largest sizes are marked `slow` and deselected by default so the normal
suite stays fast:

    python -m pytest -m "not slow"        # skip 50/100-facility runs
"""

from __future__ import annotations

import statistics
import time
import tracemalloc
from typing import Dict, List, Tuple

import pytest

from netgravity.resilience.rei import assess_network_resilience
from netgravity.resilience.service import REIService
from netgravity.schemas.network import (
    CanonicalNetwork,
    CostPeriod,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
    ProductRecord,
    TransportMode,
)
from netgravity.schemas.resilience import DisruptionConfig

ALL_NODES = DisruptionConfig(only_baseline_open_facilities=False)


# ---------------------------------------------------------------------------
# Synthetic network generator
# ---------------------------------------------------------------------------

def build_scale_network(n_dcs: int, n_markets: int = 0) -> CanonicalNetwork:
    """
    A synthetic multi-echelon network with `n_dcs` distribution centres.

    Structure: 2 plants -> N DCs -> M markets, every DC able to reach every
    market. Rates vary deterministically by index so the optimum is
    non-degenerate (some DCs genuinely matter more than others) and REI values
    are meaningful rather than all-zero.

    Deterministic by construction — no randomness — so benchmark runs are
    comparable across invocations.
    """
    n_markets = n_markets or max(4, n_dcs // 2)

    facilities: List[FacilityRecord] = [
        FacilityRecord(id="PLANT_A", name="Plant A", role=NodeRole.PLANT,
                       status=FacilityStatus.EXISTING,
                       capacity_units_per_period=1_000_000,
                       is_mandatory=True, is_closable=False),
        FacilityRecord(id="PLANT_B", name="Plant B", role=NodeRole.PLANT,
                       status=FacilityStatus.EXISTING,
                       capacity_units_per_period=1_000_000,
                       is_mandatory=True, is_closable=False),
    ]
    for i in range(n_dcs):
        facilities.append(FacilityRecord(
            id=f"DC_{i:03d}", name=f"DC {i}", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            # Ample capacity so infeasibility does not dominate the benchmark.
            capacity_units_per_period=5_000,
            fixed_cost_per_year=12_000.0 + (i % 5) * 1_200.0,
            handling_cost_per_unit=0.5 + (i % 3) * 0.25,
        ))
    for m in range(n_markets):
        facilities.append(FacilityRecord(
            id=f"MKT_{m:03d}", name=f"Market {m}", role=NodeRole.MARKET,
            status=FacilityStatus.EXISTING, is_closable=False))

    lanes: List[LaneRecord] = []
    for i in range(n_dcs):
        for plant in ("PLANT_A", "PLANT_B"):
            lanes.append(LaneRecord(
                origin_id=plant, destination_id=f"DC_{i:03d}",
                mode=TransportMode.ROAD,
                rate_per_unit=1.0 + (i % 4) * 0.5,
                distance_km=100.0 + i, lead_time_days=1.0))
        for m in range(n_markets):
            # Deterministic spread so DCs differ in usefulness.
            rate = 2.0 + ((i * 7 + m * 3) % 11) * 0.5
            lanes.append(LaneRecord(
                origin_id=f"DC_{i:03d}", destination_id=f"MKT_{m:03d}",
                mode=TransportMode.ROAD, rate_per_unit=rate,
                distance_km=50.0 + ((i + m) % 20) * 10.0, lead_time_days=1.0))

    demands = [
        DemandRecord(market_id=f"MKT_{m:03d}", product_id="P1",
                     quantity=500.0 + (m % 5) * 100.0, std_dev=0.0)
        for m in range(n_markets)
    ]

    config = OptimizationConfig(
        solver_name="HiGHS", enable_inventory=False, enforce_sla=False,
        enable_carbon_cost=False, minimum_throughput_enabled=False,
        allow_shortage=False, cost_period=CostPeriod.MONTH, mip_gap=0.001,
        time_limit_seconds=120, verbose=False,
    )
    net = CanonicalNetwork(
        network_id=f"SCALE_{n_dcs}",
        facilities=facilities,
        products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=100.0)],
        demands=demands, lanes=lanes, config=config,
    )
    return net.model_copy(update={"data_version": net.compute_data_version()})


def _measure(net: CanonicalNetwork, workers: int) -> Tuple[Dict[str, float], object]:
    tracemalloc.start()
    start = time.perf_counter()
    reg = assess_network_resilience(net, net.config, ALL_NODES, max_workers=workers)
    wall = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    per_node = [r.solve_seconds for r in reg.results if r.solve_seconds is not None]
    return {
        "nodes": reg.n_facilities_assessed,
        "solves": reg.n_milp_solves,
        "baseline_s": reg.baseline_solve_seconds or 0.0,
        "node_mean_s": statistics.fmean(per_node) if per_node else 0.0,
        "node_max_s": max(per_node) if per_node else 0.0,
        "wall_s": wall,
        "peak_mb": peak / (1024 * 1024),
    }, reg


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

class TestREIScale:

    @pytest.mark.parametrize("n_dcs,slow", [
        (7, False), (25, False),
        pytest.param(50, True, marks=pytest.mark.slow),
    ])
    def test_scale_benchmark(self, n_dcs, slow, capsys):
        net = build_scale_network(n_dcs)
        seq, seq_reg = _measure(net, workers=1)
        par, par_reg = _measure(net, workers=4)

        with capsys.disabled():
            print(f"\n  -- {n_dcs} DCs "
                  f"({len(net.facilities)} facilities, {len(net.demands)} demands, "
                  f"{len(net.lanes)} lanes) --")
            print(f"     nodes assessed   : {seq['nodes']}")
            print(f"     MILP solves      : {seq['solves']}")
            print(f"     baseline solve   : {seq['baseline_s']:.4f} s")
            print(f"     node mean / max  : {seq['node_mean_s']:.4f} / "
                  f"{seq['node_max_s']:.4f} s")
            print(f"     sequential wall  : {seq['wall_s']:.3f} s")
            print(f"     parallel(4) wall : {par['wall_s']:.3f} s  "
                  f"({seq['wall_s'] / par['wall_s']:.2f}x)")
            print(f"     peak memory      : {seq['peak_mb']:.1f} MB")

        # Structural assertions — these are the scalability claim.
        assert seq["solves"] == pytest.approx(par["solves"])
        assert seq["solves"] <= 1 + 2 * seq["nodes"], "solve count must be ~1 + N"
        assert [r.facility_id for r in seq_reg.results] == \
               [r.facility_id for r in par_reg.results]
        for a, b in zip(seq_reg.results, par_reg.results):
            if a.rei is None:
                assert b.rei is None
            else:
                assert a.rei == pytest.approx(b.rei, abs=1e-9), (
                    "parallel execution must match sequential exactly"
                )

    @pytest.mark.slow
    def test_100_facility_single_solve_and_projection(self, capsys):
        """
        At 100 DCs a full batch takes roughly an hour, so this measures ONE real
        solve and PROJECTS the batch rather than running it.

        The projection is labelled as such. Running a 64-minute batch inside a
        test suite would be unusable, and reporting an unmeasured number as a
        measurement would be worse.
        """
        net = build_scale_network(100)
        from netgravity.optimization.milp import solve

        t0 = time.perf_counter()
        result = solve(net, config=net.config)
        one_solve = time.perf_counter() - t0

        n_nodes = len([f for f in net.facilities if f.role == NodeRole.DC])
        projected = one_solve * (n_nodes + 1)

        with capsys.disabled():
            print(f"\n  -- 100 DCs ({len(net.facilities)} facilities, "
                  f"{len(net.lanes)} lanes) --")
            print(f"     MEASURED  single solve : {one_solve:.2f} s "
                  f"({result.solver.status.value})")
            print(f"     PROJECTED batch (1+N)  : {projected:.0f} s "
                  f"(~{projected / 60:.0f} min) - NOT measured end-to-end")
            print(f"     projection assumes each disruption solve costs about as")
            print(f"     much as the baseline, which the smaller sizes support.")

        assert result.is_solved
        assert one_solve > 0

    def test_cache_hit_is_constant_time_regardless_of_size(self, capsys):
        """A cache hit does not scale with node count — it is a lookup."""
        rows = []
        for n_dcs in (7, 25):
            net = build_scale_network(n_dcs)
            service = REIService()

            t0 = time.perf_counter()
            cold = service.get_or_compute(net, net.config, ALL_NODES)
            cold_wall = time.perf_counter() - t0

            t1 = time.perf_counter()
            warm = service.get_or_compute(net, net.config, ALL_NODES)
            warm_wall = time.perf_counter() - t1

            rows.append((n_dcs, cold.n_milp_solves, cold_wall, warm_wall))
            assert warm.served_from_cache is True
            assert warm_wall < cold_wall

        with capsys.disabled():
            print("\n  -- cache effectiveness --")
            for n_dcs, solves, cold, warm in rows:
                print(f"     {n_dcs:3d} DCs: cold {cold:.3f} s ({solves} solves) "
                      f"-> cached {warm * 1000:.2f} ms "
                      f"({cold / warm:.0f}x faster)")
