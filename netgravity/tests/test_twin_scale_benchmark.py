"""
NetGravity — Digital Twin scale benchmark.

Measures state construction, serialisation, retrieval, comparison and delta
compression at 7 / 25 / 50 / 100 facilities, so scalability claims rest on
measurement rather than extrapolation from Case-16.

Run the report:

    python -m pytest netgravity/tests/test_twin_scale_benchmark.py -s -k scale

Timing is REPORTED, not asserted — wall-clock thresholds are flaky on shared
hardware and would fail for reasons that have nothing to do with the code. What
IS asserted are the structural claims the timings exist to support:

  * state size grows with the network, not with the number of scenarios;
  * a scenario delta is a fraction of a full state;
  * comparison and retrieval stay correct at every size;
  * N scenarios on one baseline hold exactly one copy of the network.

The largest sizes are marked `slow` and deselected by default:

    python -m pytest -m "not slow"

Reuses `build_scale_network` from the REI benchmark rather than growing a
second synthetic generator — one definition of "a 50-facility network" keeps
the two benchmarks comparable.
"""

from __future__ import annotations

import time
import tracemalloc
from typing import Any, Dict, List, Tuple

import pytest

from netgravity.metrics.contracts import build_network_state_result
from netgravity.optimization.milp import solve as milp_solve
from netgravity.orchestrator.schemas.twin import (
    DigitalTwinState,
    StorageMode,
    TwinStateType,
)
from netgravity.orchestrator.twin import (
    DigitalTwinService,
    build_twin_state,
    to_delta,
)
from netgravity.schemas.network import CanonicalNetwork
from netgravity.tests.test_rei_scale_benchmark import build_scale_network

#: (label, n_dcs, n_markets). Case-16 sits at the small end deliberately: a
#: benchmark whose smallest point is the production target proves nothing about
#: growth.
SIZES: List[Tuple[str, int, int]] = [
    ("case16-small", 5, 4),
    ("25-facilities", 23, 12),
    ("50-facilities", 48, 25),
    ("100-facilities", 98, 50),
]

SLOW_FROM = 48   # n_dcs at or above this is marked slow


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

def _solved_state(network: CanonicalNetwork) -> Any:
    """Solve once and return the frozen `NetworkStateResult` contract."""
    result = milp_solve(network, network.config, None)
    return build_network_state_result(result, network, network.config, None)


def _closed_variant(state: Any, facility_id: str) -> Any:
    """
    A scenario-shaped state: the same solved network with one facility closed.

    Built by editing the CONTRACT rather than re-solving, because this benchmark
    measures the twin, not the MILP. Re-solving N times per size would swamp the
    twin's own numbers with solver time and measure the wrong thing.
    """
    facilities = []
    for f in state.facilities:
        if f.facility_id == facility_id:
            facilities.append(f.model_copy(update={
                "is_open": False, "throughput_units": 0.0, "utilization_pct": 0.0,
            }))
        else:
            facilities.append(f)
    flows = [f for f in state.flows if f.origin_id != facility_id
             and f.destination_id != facility_id]
    return state.model_copy(update={"facilities": facilities, "flows": flows})


def _measure(fn, *args, **kwargs) -> Tuple[Any, float]:
    start = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, time.perf_counter() - start


def _state_bytes(state: DigitalTwinState) -> int:
    return len(state.model_dump_json().encode("utf-8"))


SIZE_PARAMS = [
    pytest.param(label, n_dcs, n_mkts,
                 marks=([pytest.mark.slow] if n_dcs >= SLOW_FROM else []),
                 id=label)
    for label, n_dcs, n_mkts in SIZES
]


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,n_dcs,n_markets", SIZE_PARAMS)
def test_scale_twin_state_lifecycle(label: str, n_dcs: int, n_markets: int, capsys):
    """
    Full lifecycle at one network size: build → publish → read → compare.

    Reports construction, serialisation, retrieval, comparison and peak memory.
    Asserts correctness at every size — a benchmark that only times things
    cannot tell a fast answer from a fast wrong answer.
    """
    network = build_scale_network(n_dcs, n_markets)
    state_contract = _solved_state(network)
    n_facilities = len(state_contract.facilities)
    n_flows = len(state_contract.flows)

    tracemalloc.start()

    baseline, build_s = _measure(
        build_twin_state,
        snapshot_id=f"snap_{label}",
        state_type=TwinStateType.OPTIMIZED,
        network_state=state_contract,
        execution_id="bench",
    )

    payload, serialise_s = _measure(_state_bytes, baseline)

    service = DigitalTwinService()
    _, publish_s = _measure(service.update, baseline)

    _, retrieve_s = _measure(service.get_by_id, baseline.state_id, flow_limit=0)
    _, summary_s = _measure(service.get_by_id, baseline.state_id, include_flows=False)

    # One scenario: close the first DC.
    target = next(f.facility_id for f in state_contract.facilities
                  if f.facility_id.startswith("DC_"))
    scenario_contract = _closed_variant(state_contract, target)
    scenario = build_twin_state(
        snapshot_id=f"snap_{label}",
        state_type=TwinStateType.SCENARIO,
        network_state=scenario_contract,
        scenario_id="scn_bench",
        execution_id="bench",
    )
    _, compress_s = _measure(service.update, scenario)
    stored_scenario = service.store.get(scenario.state_id)

    comparison, compare_s = _measure(
        service.compare, baseline.state_id, scenario.state_id,
    )

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(
        f"\n[twin-scale] {label:<16} facilities={n_facilities:>3} flows={n_flows:>5}"
        f"\n             build={build_s * 1000:8.2f}ms  serialise={serialise_s * 1000:8.2f}ms"
        f"  payload={payload / 1024:8.1f}KB"
        f"\n             publish={publish_s * 1000:8.2f}ms  retrieve={retrieve_s * 1000:8.2f}ms"
        f"  summary={summary_s * 1000:8.2f}ms"
        f"\n             compress={compress_s * 1000:8.2f}ms compare={compare_s * 1000:8.2f}ms"
        f"  peak={peak / 1024 / 1024:6.2f}MB"
        f"\n             delta stored: {len(stored_scenario.facilities)} facilities,"
        f" {len(stored_scenario.flows)} flows"
        f" ({len(stored_scenario.removed_lane_keys)} lanes removed)"
    )

    # ---- correctness, at every size --------------------------------------
    assert n_facilities >= n_dcs
    assert baseline.kpis is not None
    assert baseline.flow_aggregate is not None
    assert baseline.flow_aggregate.total_lanes == n_flows

    view = service.get_by_id(baseline.state_id, flow_limit=0)
    assert len(view.facilities) == n_facilities
    assert view.flows.total == n_flows

    assert stored_scenario.storage_mode is StorageMode.DELTA
    assert comparison.facilities_closed == [target]

    materialised = service.materialize(scenario.state_id)
    assert len(materialised.facilities) == n_facilities


@pytest.mark.parametrize("label,n_dcs,n_markets", SIZE_PARAMS)
def test_scale_scenario_delta_is_a_fraction_of_a_full_state(
    label: str, n_dcs: int, n_markets: int, capsys,
):
    """
    The scalability claim that matters: a scenario costs a delta, not a copy.

    Asserted as a ratio rather than an absolute size, so it holds at every
    network size instead of encoding one machine's numbers.
    """
    network = build_scale_network(n_dcs, n_markets)
    state_contract = _solved_state(network)
    target = next(f.facility_id for f in state_contract.facilities
                  if f.facility_id.startswith("DC_"))

    baseline = build_twin_state(
        snapshot_id=f"snap_{label}", state_type=TwinStateType.OPTIMIZED,
        network_state=state_contract,
    )
    full_scenario = build_twin_state(
        snapshot_id=f"snap_{label}", state_type=TwinStateType.SCENARIO,
        network_state=_closed_variant(state_contract, target),
        scenario_id="scn_bench",
    )
    delta = to_delta(full_scenario, baseline)

    full_bytes = _state_bytes(full_scenario)
    delta_bytes = _state_bytes(delta)
    ratio = delta_bytes / full_bytes

    print(
        f"\n[twin-delta] {label:<16} full={full_bytes / 1024:8.1f}KB"
        f"  delta={delta_bytes / 1024:8.1f}KB  ratio={ratio:5.1%}"
        f"  ({len(delta.facilities)}/{len(full_scenario.facilities)} facilities,"
        f" {len(delta.flows)}/{len(full_scenario.flows)} flows)"
    )

    assert delta.storage_mode is StorageMode.DELTA
    # A single closure touches a small share of a large network. The bound is
    # deliberately loose — the claim is "materially smaller", not a precise
    # figure that would break on a different network shape.
    assert ratio < 0.75, f"delta is {ratio:.1%} of a full state"
    assert len(delta.facilities) < len(full_scenario.facilities)


@pytest.mark.parametrize("label,n_dcs,n_markets", SIZE_PARAMS)
def test_scale_many_scenarios_hold_one_copy_of_the_network(
    label: str, n_dcs: int, n_markets: int, capsys,
):
    """
    Ten scenarios on one baseline must not cost ten networks.

    This is the requirement "do not duplicate the entire CanonicalNetwork for
    every scenario", measured. Nothing here holds a network at all — states
    reference `snapshot_id` — so what is being checked is that the STATE
    payloads stay small too.
    """
    network = build_scale_network(n_dcs, n_markets)
    state_contract = _solved_state(network)
    dc_ids = [f.facility_id for f in state_contract.facilities
              if f.facility_id.startswith("DC_")][:10]

    service = DigitalTwinService()
    baseline = build_twin_state(
        snapshot_id=f"snap_{label}", state_type=TwinStateType.OPTIMIZED,
        network_state=state_contract,
    )
    service.update(baseline)
    baseline_bytes = _state_bytes(baseline)

    total_scenario_bytes = 0
    for index, target in enumerate(dc_ids):
        scenario = build_twin_state(
            snapshot_id=f"snap_{label}", state_type=TwinStateType.SCENARIO,
            network_state=_closed_variant(state_contract, target),
            scenario_id=f"scn_{index}",
        )
        service.update(scenario)
        total_scenario_bytes += _state_bytes(service.store.get(scenario.state_id))

    naive_bytes = baseline_bytes * len(dc_ids)
    print(
        f"\n[twin-fanout] {label:<15} baseline={baseline_bytes / 1024:8.1f}KB"
        f"  {len(dc_ids)} scenarios stored={total_scenario_bytes / 1024:8.1f}KB"
        f"  vs {naive_bytes / 1024:8.1f}KB if copied"
        f"  ({total_scenario_bytes / naive_bytes:5.1%})"
    )

    assert len(service.list_scenarios(f"snap_{label}")) == len(dc_ids)
    assert total_scenario_bytes < naive_bytes

    # Every scenario still materialises to the whole network, independently.
    for index, target in enumerate(dc_ids):
        full = service.materialize(f"tws_snap_{label}_scn_{index}")
        assert len(full.facilities) == len(state_contract.facilities)
        closed = {f.facility_id for f in full.facilities if not f.is_open}
        assert target in closed, f"scenario {index} lost its own closure"


@pytest.mark.slow
def test_scale_retrieval_is_bounded_by_page_size_not_network_size(capsys):
    """
    A default read returns one page whatever the network size.

    The property pagination exists for: the common case must not pay for the
    rare one.
    """
    service = DigitalTwinService()
    sizes: Dict[str, int] = {}

    for label, n_dcs, n_markets in SIZES:
        network = build_scale_network(n_dcs, n_markets)
        state = build_twin_state(
            snapshot_id=f"snap_page_{label}", state_type=TwinStateType.OPTIMIZED,
            network_state=_solved_state(network),
        )
        service.update(state)
        view, elapsed = _measure(service.get_by_id, state.state_id)
        sizes[label] = view.flows.total
        print(
            f"\n[twin-page] {label:<16} lanes={view.flows.total:>5}"
            f"  returned={len(view.flows.items):>4}  {elapsed * 1000:7.2f}ms"
        )
        assert len(view.flows.items) <= 500

    assert sizes["100-facilities"] > sizes["case16-small"]


# ---------------------------------------------------------------------------
# Dense flow sets
# ---------------------------------------------------------------------------

#: A cost-minimising optimum is SPARSE — 100 facilities above produced only 61
#: lanes, because using a lane costs money. That is a real property of solved
#: networks, and it means the benchmarks above never stress the flow path.
#: Multi-period, multi-product or multi-modal networks produce far denser sets,
#: so these cases build the flow set directly rather than solving for it.
#: Synthetic, and labelled as such: this measures the twin's flow handling, and
#: makes no claim about what any particular solve produces.
DENSE_FLOW_SIZES = [
    pytest.param(2_000, id="2k-lanes"),
    pytest.param(20_000, marks=pytest.mark.slow, id="20k-lanes"),
    pytest.param(50_000, marks=pytest.mark.slow, id="50k-lanes"),
]


def _dense_state(snapshot_id: str, n_lanes: int, *,
                 scenario_id: str = None) -> DigitalTwinState:
    """A state with a synthetic dense flow set and 100 facilities."""
    from netgravity.orchestrator.schemas.twin import (
        FacilityState,
        FlowState,
        TwinKPIs,
        TwinProvenance,
    )
    from netgravity.orchestrator.twin import build_flow_aggregate

    n_origins = 100
    flows = [
        FlowState(
            origin_id=f"DC_{i % n_origins:03d}",
            destination_id=f"MKT_{i:05d}",
            flow_units=float(100 + (i % 37)),
            transport_cost=float(200 + (i % 53)),
            distance_km=float(50 + (i % 400)),
            carbon_kg=float(i % 19),
        )
        for i in range(n_lanes)
    ]
    facilities = [
        FacilityState(
            facility_id=f"DC_{i:03d}", facility_name=f"DC {i}", role="DC",
            is_open=True, throughput_units=float(1_000 + i),
            capacity_units=5_000.0, utilization_pct=float(20 + i % 60),
        )
        for i in range(n_origins)
    ]
    state_type = (TwinStateType.SCENARIO if scenario_id
                  else TwinStateType.OPTIMIZED)
    return DigitalTwinState(
        state_id=f"tws_{snapshot_id}_" + (scenario_id or "optimized"),
        snapshot_id=snapshot_id, scenario_id=scenario_id, state_type=state_type,
        provenance=TwinProvenance(snapshot_id=snapshot_id, scenario_id=scenario_id),
        facilities=facilities, flows=flows,
        flow_aggregate=build_flow_aggregate(flows),
        kpis=TwinKPIs(business_network_cost=1_000_000.0, total_demand=500_000.0),
    )


@pytest.mark.parametrize("n_lanes", DENSE_FLOW_SIZES)
def test_scale_dense_flow_sets_paginate_and_aggregate(n_lanes: int, capsys):
    """
    Pagination and aggregation on a genuinely large flow set.

    Asserts the property the numbers exist to support: a default read costs the
    same whatever the flow count, while the aggregate still describes all of it.
    """
    service = DigitalTwinService()
    state, build_s = _measure(_dense_state, f"snap_dense_{n_lanes}", n_lanes)
    payload = _state_bytes(state)
    _, publish_s = _measure(service.update, state)

    default_view, default_s = _measure(service.get_by_id, state.state_id)
    summary_view, summary_s = _measure(
        service.get_by_id, state.state_id, include_flows=False,
    )
    full_view, full_s = _measure(service.get_by_id, state.state_id, flow_limit=0)
    last_page, page_s = _measure(
        service.get_by_id, state.state_id,
        flow_offset=max(0, n_lanes - 100), flow_limit=100,
    )

    print(
        f"\n[twin-dense] {n_lanes:>6} lanes  build={build_s * 1000:8.2f}ms"
        f"  payload={payload / 1024 / 1024:6.2f}MB  publish={publish_s * 1000:8.2f}ms"
        f"\n              default-page={default_s * 1000:8.2f}ms"
        f"  summary={summary_s * 1000:8.2f}ms  last-page={page_s * 1000:8.2f}ms"
        f"  all-lanes={full_s * 1000:8.2f}ms"
    )

    # A default read returns one page regardless of total size.
    assert len(default_view.flows.items) == 500
    assert default_view.flows.total == n_lanes
    assert default_view.flows.has_more is True

    # The summary path returns no lanes but still describes every one of them.
    assert summary_view.flows.items == []
    assert summary_view.flow_aggregate is not None
    assert summary_view.flow_aggregate.total_lanes == n_lanes
    assert summary_view.flow_aggregate.total_flow_units == pytest.approx(
        sum(f.flow_units for f in state.flows), rel=1e-9,
    )
    assert len(summary_view.flow_aggregate.units_by_origin) == 100

    # Paging to the end works and reports itself exhausted.
    assert len(last_page.flows.items) == 100
    assert last_page.flows.has_more is False
    assert len(full_view.flows.items) == n_lanes

    # The summary path must not cost what the full read costs — that is the
    # whole reason it exists. Ratio, not a wall-clock threshold.
    assert summary_s < full_s


@pytest.mark.slow
def test_scale_dense_comparison_and_delta(capsys):
    """
    Comparison and delta compression on a dense flow set.

    The scenario reroutes a tenth of the lanes, which is the realistic shape: a
    closure moves some volume, not all of it.
    """
    n_lanes = 20_000
    base = _dense_state("snap_densecmp", n_lanes)

    rerouted = [
        f.model_copy(update={"flow_units": f.flow_units * 1.5})
        if index % 10 == 0 else f
        for index, f in enumerate(base.flows)
    ]
    scenario = base.model_copy(update={
        "state_id": "tws_snap_densecmp_scn_1",
        "scenario_id": "scn_1",
        "state_type": TwinStateType.SCENARIO,
        "flows": rerouted,
    })

    service = DigitalTwinService()
    service.update(base)
    _, compress_s = _measure(service.update, scenario)
    stored = service.store.get(scenario.state_id)

    comparison, compare_s = _measure(
        service.compare, base.state_id, scenario.state_id,
    )
    _, materialise_s = _measure(service.materialize, scenario.state_id)

    print(
        f"\n[twin-dense-cmp] {n_lanes} lanes"
        f"  compress={compress_s * 1000:8.2f}ms  compare={compare_s * 1000:8.2f}ms"
        f"  materialise={materialise_s * 1000:8.2f}ms"
        f"\n                 delta stored {len(stored.flows)}/{n_lanes} lanes"
        f"  ({_state_bytes(stored) / _state_bytes(scenario):5.1%} of full)"
    )

    assert stored.storage_mode is StorageMode.DELTA
    assert len(stored.flows) == n_lanes // 10
    assert len(comparison.lane_changes) == n_lanes // 10
    assert all(c.change == "INCREASED" for c in comparison.lane_changes)
    assert len(service.materialize(scenario.state_id).flows) == n_lanes
