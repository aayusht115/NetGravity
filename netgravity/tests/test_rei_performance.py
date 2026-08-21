"""
NetGravity — REI performance benchmark.

Measures the real cost of a REI batch rather than guessing at it, so the
scalability decision (sequential vs parallel vs distributed) rests on numbers.

Measured per run:
    baseline solve time
    per-disruption solve time (min / mean / max)
    total batch wall time
    number of MILP solves
    sequential vs parallel wall time
    cache hit cost

Run with `-s` to see the report:

    python -m pytest netgravity/tests/test_rei_performance.py -s -k benchmark

Assertions are deliberately loose — this is a benchmark, and tight timing
assertions would make the suite flaky on shared CI hardware. It asserts
CORRECTNESS invariants (solve counts, parallel/sequential equivalence) and
merely reports timings.
"""

from __future__ import annotations

import statistics
import time
from typing import Dict, List

import pytest

from netgravity.resilience.rei import assess_network_resilience
from netgravity.resilience.service import REIService
from netgravity.schemas.network import NodeRole
from netgravity.schemas.resilience import DisruptionConfig
from netgravity.tests.fixtures.case16_synthetic import build_case16_network

ALL_NODES = DisruptionConfig(only_baseline_open_facilities=False)


def _profile(registry) -> Dict[str, float]:
    per_node = [r.solve_seconds for r in registry.results if r.solve_seconds is not None]
    return {
        "nodes": registry.n_facilities_assessed,
        "milp_solves": registry.n_milp_solves,
        "baseline_s": registry.baseline_solve_seconds or 0.0,
        "node_min_s": min(per_node) if per_node else 0.0,
        "node_mean_s": statistics.fmean(per_node) if per_node else 0.0,
        "node_max_s": max(per_node) if per_node else 0.0,
        "total_s": registry.total_assessment_seconds or 0.0,
    }


class TestREIPerformance:

    def test_benchmark_sequential_vs_parallel(self, capsys):
        """Profile a full batch and report. Correctness is asserted; timing is not."""
        net = build_case16_network()

        seq_start = time.perf_counter()
        seq = assess_network_resilience(net, net.config, ALL_NODES, max_workers=1)
        seq_wall = time.perf_counter() - seq_start

        par_start = time.perf_counter()
        par = assess_network_resilience(net, net.config, ALL_NODES, max_workers=4)
        par_wall = time.perf_counter() - par_start

        sp = _profile(seq)
        pp = _profile(par)

        with capsys.disabled():
            print("\n" + "=" * 68)
            print("REI PERFORMANCE BENCHMARK — Case-16 synthetic")
            print("=" * 68)
            print(f"  network            : {net.network_id} "
                  f"({len(net.facilities)} facilities, {len(net.demands)} demands, "
                  f"{len(net.lanes)} lanes)")
            print(f"  nodes assessed     : {sp['nodes']}")
            print(f"  MILP solves        : {sp['milp_solves']}  (1 baseline + N nodes "
                  f"+ diagnostics)")
            print("  ---- sequential (max_workers=1) ----")
            print(f"    baseline solve   : {sp['baseline_s']:.4f} s")
            print(f"    per node min/mean/max : {sp['node_min_s']:.4f} / "
                  f"{sp['node_mean_s']:.4f} / {sp['node_max_s']:.4f} s")
            print(f"    batch total      : {sp['total_s']:.4f} s")
            print(f"    wall clock       : {seq_wall:.4f} s")
            print("  ---- parallel (max_workers=4) ----")
            print(f"    batch total      : {pp['total_s']:.4f} s")
            print(f"    wall clock       : {par_wall:.4f} s")
            speedup = seq_wall / par_wall if par_wall > 0 else float("nan")
            print(f"    speed-up         : {speedup:.2f}x")
            print("  NOTE: threads do help here — highspy releases the GIL inside the")
            print("        solve, so independent scenarios genuinely overlap. Speed-up")
            print("        is bounded by core count and by the serial baseline solve.")
            print("        The max_workers seam also lets a process pool or remote")
            print("        worker replace threads without touching REI domain logic.")
            print("=" * 68)

        # Correctness invariants — these DO get asserted.
        assert sp["milp_solves"] == pp["milp_solves"]
        assert [r.facility_id for r in seq.results] == [r.facility_id for r in par.results]
        for a, b in zip(seq.results, par.results):
            if a.rei is None:
                assert b.rei is None
            else:
                assert a.rei == pytest.approx(b.rei, abs=1e-9)

    def test_solve_count_is_one_plus_n(self):
        """The headline scalability fact: 1 baseline + N disruptions."""
        net = build_case16_network()
        calls: List[str] = []

        def counting_solve(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            calls.append(scenario_id or "?")
            return solve(network, config=config, scenario_id=scenario_id)

        reg = assess_network_resilience(net, net.config, ALL_NODES,
                                        solve_fn=counting_solve)

        baseline_calls = [c for c in calls if c == "REI_BASELINE"]
        disrupt_calls = [c for c in calls if c.startswith("REI_DISRUPT_")]
        diag_calls = [c for c in calls if c.startswith("REI_SERVICE_DIAG_")]

        assert len(baseline_calls) == 1, "the baseline must be solved exactly once"
        assert len(disrupt_calls) == reg.n_facilities_assessed
        # One disruption solve per node, no duplicates.
        assert len(set(disrupt_calls)) == len(disrupt_calls)
        assert len(calls) == 1 + len(disrupt_calls) + len(diag_calls)
        assert reg.n_milp_solves == len(calls)

    def test_cache_avoids_all_solves(self, capsys):
        """A valid cached batch costs zero solves."""
        service = REIService()
        net = build_case16_network()

        cold_start = time.perf_counter()
        cold = service.get_or_compute(net, net.config, ALL_NODES)
        cold_wall = time.perf_counter() - cold_start

        warm_start = time.perf_counter()
        warm = service.get_or_compute(net, net.config, ALL_NODES)
        warm_wall = time.perf_counter() - warm_start

        with capsys.disabled():
            print(f"\n  cold batch : {cold_wall:.4f} s "
                  f"({cold.n_milp_solves} MILP solves)")
            print(f"  cached     : {warm_wall:.4f} s (0 MILP solves)")
            print(f"  solves avoided per reuse : {service.stats.milp_solves_avoided}")

        assert cold.served_from_cache is False
        assert warm.served_from_cache is True
        assert service.stats.milp_solves_executed == cold.n_milp_solves
        assert service.stats.milp_solves_avoided == cold.n_milp_solves
        assert warm_wall < cold_wall, "a cache hit must be cheaper than recomputing"

    def test_batch_scales_linearly_in_node_count(self, capsys):
        """
        Total solves grow as 1 + N, not N².

        Compares a DC-only batch against an all-node batch on the same network:
        wall time should scale with the node count, confirming the baseline is
        not being re-solved per node.
        """
        net = build_case16_network()

        small = assess_network_resilience(
            net, net.config,
            DisruptionConfig(eligible_roles=[NodeRole.DC],
                             only_baseline_open_facilities=False))
        large = assess_network_resilience(net, net.config, ALL_NODES)

        with capsys.disabled():
            print(f"\n  DC-only batch : {small.n_facilities_assessed} nodes, "
                  f"{small.n_milp_solves} solves, "
                  f"{small.total_assessment_seconds:.4f} s")
            print(f"  all-node batch: {large.n_facilities_assessed} nodes, "
                  f"{large.n_milp_solves} solves, "
                  f"{large.total_assessment_seconds:.4f} s")

        assert large.n_facilities_assessed >= small.n_facilities_assessed
        # Solve count tracks node count exactly (plus diagnostics), never N².
        assert large.n_milp_solves <= 1 + 2 * large.n_facilities_assessed
