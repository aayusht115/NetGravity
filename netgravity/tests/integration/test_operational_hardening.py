"""
The operational claims from the last report, tested rather than asserted.

Each class here covers one item that was listed as "still not true":

  * rate-limit counters were per process, so N workers gave one caller N
    budgets — and the limit loosened by exactly the factor you scaled out by;
  * execution traces lived in a 500-entry ring buffer in memory, so a restart
    kept the answers and lost the workings;
  * a forecast was produced over up to 24 periods and exactly one of them ever
    reached the optimiser;
  * the per-facility KPI endpoint's own docstring promised resilience and risk
    blocks that its workflow never computed.
"""

from __future__ import annotations

import os
import pathlib
import threading

import pytest


# ===========================================================================
# Shared rate-limit counters
# ===========================================================================

class TestRateLimitCountersAreShared:

    def test_the_database_store_counts_one_budget_across_threads(self, tmp_path):
        """
        Four callers hitting one bucket simultaneously must consume ONE budget.

        With per-process counters and four workers they consumed four. That is
        the wrong direction of failure: the limit exists to stop one caller
        occupying every worker, and adding workers to survive that load was
        what loosened it.
        """
        from app.backend.services.ratelimit import RateLimiter, _DatabaseStore

        limiter = RateLimiter()
        limiter.use_shared_store()
        limiter.reset()

        allowed = []
        lock = threading.Lock()

        def hammer():
            local = []
            for _ in range(10):
                ok, _, _ = limiter.check("test.shared", "client-a",
                                         limit=25, window_seconds=60)
                local.append(ok)
            with lock:
                allowed.extend(local)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(allowed) == 40
        assert sum(allowed) == 25, (
            f"25 of 40 requests should have been allowed, not {sum(allowed)}")
        assert isinstance(limiter._store, _DatabaseStore)  # noqa: SLF001
        limiter.reset()

    def test_separate_clients_have_separate_budgets(self):
        from app.backend.services.ratelimit import RateLimiter

        limiter = RateLimiter()
        limiter.use_shared_store()
        limiter.reset()
        for _ in range(3):
            assert limiter.check("test.split", "a", limit=3, window_seconds=60)[0]
        assert not limiter.check("test.split", "a", limit=3, window_seconds=60)[0]
        assert limiter.check("test.split", "b", limit=3, window_seconds=60)[0], \
            "one client exhausting its budget must not limit another"
        limiter.reset()

    def test_an_unreachable_store_degrades_to_a_limit_not_to_no_limit(self):
        """
        A store that cannot be reached must not become an open door. The
        limiter falls back to a per-process window and says so.
        """
        from app.backend.services.ratelimit import RateLimiter

        class Broken:
            shared = True

            def bump(self, *args, **kwargs):
                raise RuntimeError("store is down")

            def clear(self):
                pass

        limiter = RateLimiter()
        limiter._store = Broken()  # noqa: SLF001
        results = [limiter.check("test.degraded", "c", limit=2, window_seconds=60)[0]
                   for _ in range(5)]
        assert results == [True, True, False, False, False]
        assert limiter.is_shared is False, \
            "a degraded limiter must not report itself as shared"


# ===========================================================================
# Execution traces survive the process
# ===========================================================================

class TestExecutionTracesArePersisted:

    def test_a_trace_round_trips_through_to_dict_and_back(self):
        """
        `from_dict` is the exact inverse of `to_dict`. It has to be: a trace
        read back from the store must answer the same questions as one still in
        memory.
        """
        from netgravity.orchestrator.audit.audit_logger import ExecutionTrace

        trace = ExecutionTrace(
            execution_id="exec_1", request_id="req_1", actor_id="u1",
            actor_role="PLANNER", raw_input="why is this expensive?",
            baseline_snapshot_id="snap_abc", data_version="v1",
            workflow_id="wf_x", final_status="COMPLETED",
            final_summary="a summary",
        )
        trace.record("test.event", detail_key="detail_value")
        trace.plan_steps = [{"step_id": "s1", "capability": "optimize.network"}]
        trace.scenario_ids = ["scn_1"]
        trace.errors = [{"code": "X"}]

        rebuilt = ExecutionTrace.from_dict(trace.to_dict())

        assert rebuilt.execution_id == "exec_1"
        assert rebuilt.actor_id == "u1"
        assert rebuilt.actor_role == "PLANNER"
        assert rebuilt.raw_input == "why is this expensive?"
        assert rebuilt.baseline_snapshot_id == "snap_abc"
        assert rebuilt.data_version == "v1"
        assert rebuilt.workflow_id == "wf_x"
        assert rebuilt.final_status == "COMPLETED"
        assert rebuilt.final_summary == "a summary"
        assert rebuilt.plan_steps == trace.plan_steps
        assert rebuilt.scenario_ids == ["scn_1"]
        assert rebuilt.errors == [{"code": "X"}]
        assert [e.event_type for e in rebuilt.events] == \
               [e.event_type for e in trace.events]
        assert rebuilt.to_dict() == trace.to_dict(), "the round trip must be exact"

    def test_an_evicted_trace_is_read_back_rather_than_reported_missing(self):
        """
        The ring buffer held 500 traces, so the 501st silently evicted the 1st.
        With a sink attached an eviction costs a lookup, not the trail.
        """
        from netgravity.orchestrator.audit.audit_logger import (
            AuditLogger, ExecutionTrace,
        )

        stored = {}

        class Sink:
            def save(self, trace):
                stored[trace.execution_id] = trace.to_dict()

            def load(self, execution_id):
                return stored.get(execution_id)

            def recent(self, n):
                return list(stored.values())[-n:]

        logger = AuditLogger(max_traces=1, sink=Sink())
        assert logger.is_durable

        first = ExecutionTrace(execution_id="exec_first", actor_id="u")
        logger._traces["exec_first"] = first        # noqa: SLF001
        logger._order.append("exec_first")          # noqa: SLF001
        Sink().save(first)

        # Push it out of the buffer.
        logger._traces.clear()                      # noqa: SLF001
        logger._order.clear()                       # noqa: SLF001

        recovered = logger.get("exec_first")
        assert recovered is not None, "an evicted trace must be readable"
        assert recovered.execution_id == "exec_first"

    def test_a_failing_sink_never_breaks_an_execution(self):
        """
        A trace that cannot be stored is logged. The execution has already
        succeeded and must still return its answer.
        """
        from netgravity.orchestrator.audit.audit_logger import AuditLogger

        class Exploding:
            def save(self, trace):
                raise RuntimeError("disk full")

            def load(self, execution_id):
                raise RuntimeError("disk full")

            def recent(self, n):
                raise RuntimeError("disk full")

        logger = AuditLogger(sink=Exploding())
        assert logger.get("nothing") is None      # swallowed, not raised
        assert logger.recent(5) == []

    def test_traces_survive_a_write_and_read_through_persistence(self, tmp_path):
        """The real sink, against the real store."""
        from app.backend.services import persistence
        from app.backend.services.durability import _TraceSink
        from netgravity.orchestrator.audit.audit_logger import ExecutionTrace

        trace = ExecutionTrace(
            execution_id="exec_persist", actor_id="u9", actor_role="PLANNER",
            baseline_snapshot_id="snap_9", workflow_id="wf_9",
            final_status="COMPLETED",
        )
        trace.record("stored.event")

        sink = _TraceSink()
        sink.save(trace)
        try:
            document = sink.load("exec_persist")
            assert document is not None
            rebuilt = ExecutionTrace.from_dict(document)
            assert rebuilt.execution_id == "exec_persist"
            assert rebuilt.actor_id == "u9"
            assert rebuilt.has_event("stored.event")
            assert any(d.get("execution_id") == "exec_persist"
                       for d in sink.recent(20))
        finally:
            persistence.database.execute(
                "DELETE FROM execution_traces WHERE execution_id = ?",
                ("exec_persist",))


# ===========================================================================
# The whole forecast horizon reaches the optimiser
# ===========================================================================

class TestForecastHorizonReachesTheModel:

    def _forecast(self, horizon: int):
        """A forecast covering `horizon` periods for one market and product."""
        from netgravity.forecasting.schemas import (
            ForecastPoint, ForecastProvenance, ForecastResult, ForecastStatus,
            SeriesForecast,
        )
        points = [ForecastPoint(period=p, mean=100.0 + p, p10=90.0 + p,
                                p50=100.0 + p, p90=110.0 + p, std_dev=5.0)
                  for p in range(1, horizon + 1)]
        series = SeriesForecast(
            market_id="MKT_NORTH", product_id="P1", status=ForecastStatus.OK,
            engine="test", points=points, n_history_periods=24,
        )
        return ForecastResult(
            status=ForecastStatus.OK, series=[series],
            provenance=ForecastProvenance(
                snapshot_id="snap_x", data_version="v1", horizon=horizon,
                model_version="test-1"),
        )

    def _network(self):
        from netgravity.schemas.network import (
            CanonicalNetwork, DemandRecord, FacilityRecord, FacilityStatus,
            LaneRecord, NodeRole, OptimizationConfig, ProductRecord,
            TransportMode,
        )
        return CanonicalNetwork(
            network_id="FC",
            data_version="v1",
            facilities=[
                # A DC is an intermediate node: its flow balance requires
                # inbound to equal outbound, so without a plant behind it
                # nothing can flow and the test would prove nothing.
                FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                               status=FacilityStatus.EXISTING,
                               capacity_units_per_period=9999,
                               is_mandatory=True, is_closable=False),
                FacilityRecord(id="DC", name="DC", role=NodeRole.DC,
                               status=FacilityStatus.EXISTING,
                               capacity_units_per_period=9999),
                FacilityRecord(id="MKT_NORTH", name="North", role=NodeRole.MARKET,
                               status=FacilityStatus.EXISTING),
            ],
            products=[ProductRecord(id="P1", name="P1", unit_value=5.0)],
            demands=[DemandRecord(market_id="MKT_NORTH", product_id="P1",
                                  quantity=100.0, sla_days=3.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=10.0, lead_time_days=1.0),
                LaneRecord(origin_id="DC", destination_id="MKT_NORTH",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=10.0, lead_time_days=1.0),
            ],
            config=OptimizationConfig(enforce_sla=False, allow_shortage=True,
                                      verbose=False),
        )

    def test_one_period_is_still_one_period(self):
        from netgravity.orchestrator.engines.forecast_bridge import (
            apply_forecast_to_network,
        )
        applied = apply_forecast_to_network(
            self._forecast(6), self._network(), snapshot_id="snap_x", period=1)
        assert applied.ok, applied.reasons
        assert len(applied.network.demands) == 1
        assert applied.periods == [1]

    def test_the_whole_horizon_becomes_a_multi_period_demand_table(self):
        """
        Twelve forecast periods reached the solver as one. The other eleven were
        computed, returned to the screen, and dropped on the way — which is
        exactly the seasonality the forecast exists to describe.
        """
        from netgravity.orchestrator.engines.forecast_bridge import (
            apply_forecast_horizon_to_network,
        )
        applied = apply_forecast_horizon_to_network(
            self._forecast(12), self._network(), snapshot_id="snap_x")
        assert applied.ok, applied.reasons
        assert len(applied.periods) == 12
        assert len(applied.network.demands) == 12
        assert {d.period for d in applied.network.demands} == set(range(1, 13))
        # The forecast rises by one unit a period; the demand table must too,
        # or the periods are not the forecast's periods.
        by_period = {d.period: d.quantity for d in applied.network.demands}
        assert by_period[1] == pytest.approx(101.0)
        assert by_period[12] == pytest.approx(112.0)

    def test_the_horizon_network_solves_as_a_horizon(self):
        from netgravity.optimization.milp import milp_solve
        from netgravity.orchestrator.engines.forecast_bridge import (
            apply_forecast_horizon_to_network,
        )
        applied = apply_forecast_horizon_to_network(
            self._forecast(6), self._network(), snapshot_id="snap_x")
        result = milp_solve(applied.network, None)
        assert result.period_report["modelled_periods"] == 6
        assert {f.period for f in result.flow_decisions} == set(range(1, 7))

    def test_a_pair_the_forecast_does_not_cover_is_refused_not_mixed(self):
        """
        A demand table that is forecast for one market and observed for another
        is one nobody could attribute, so REJECT refuses to build it.
        """
        from netgravity.orchestrator.engines.forecast_bridge import (
            UnforecastPolicy, apply_forecast_horizon_to_network,
        )
        elsewhere = self._forecast(6)
        elsewhere.series[0].market_id = "MKT_SOUTH"      # not in this network
        applied = apply_forecast_horizon_to_network(
            elsewhere, self._network(), snapshot_id="snap_x",
            unforecast_policy=UnforecastPolicy.REJECT)
        assert not applied.ok
        assert any("no usable forecast" in r for r in applied.reasons)

    def test_keeping_observed_demand_repeats_it_and_labels_it(self):
        from netgravity.orchestrator.engines.forecast_bridge import (
            DemandProvenance, UnforecastPolicy, apply_forecast_horizon_to_network,
        )
        elsewhere = self._forecast(4)
        elsewhere.series[0].market_id = "MKT_SOUTH"
        applied = apply_forecast_horizon_to_network(
            elsewhere, self._network(), snapshot_id="snap_x",
            periods=[1, 2, 3, 4],
            unforecast_policy=UnforecastPolicy.KEEP_OBSERVED)
        assert applied.ok, applied.reasons
        assert len(applied.network.demands) == 4
        assert all(d.quantity == pytest.approx(100.0)
                   for d in applied.network.demands), \
            "an observation describes one period and says nothing about others"
        assert set(applied.provenance.values()) == {DemandProvenance.OBSERVED}
        assert any("no seasonality of their own" in w for w in applied.warnings)

    def test_a_horizon_beyond_the_forecast_is_refused(self):
        from netgravity.orchestrator.engines.forecast_bridge import (
            apply_forecast_horizon_to_network,
        )
        applied = apply_forecast_horizon_to_network(
            self._forecast(3), self._network(), snapshot_id="snap_x",
            periods=[1, 2, 3, 4, 5])
        assert not applied.ok
        assert any("beyond the forecast horizon" in r for r in applied.reasons)


# ===========================================================================
# Analysis variants
# ===========================================================================

class TestResilienceIsCachedSeparatelyFromTheBaseline:

    def test_a_variant_is_a_different_cache_entry(self):
        from app.backend.services.analysis_store import AnalysisService

        service = AnalysisService()
        calls = []

        def compute_baseline():
            calls.append("baseline")
            return {"kpis": {"a": 1}}

        def compute_resilience():
            calls.append("resilience")
            return {"facility_resilience": {"F1": {}}}

        service.get("snap_1", "v1", compute_baseline)
        service.get("snap_1", "v1", compute_resilience, variant="resilience")
        # Both computed once, neither served from the other's entry.
        assert calls == ["baseline", "resilience"]

        service.get("snap_1", "v1", compute_baseline)
        service.get("snap_1", "v1", compute_resilience, variant="resilience")
        assert calls == ["baseline", "resilience"], "both must now be cached"

        assert service.peek("snap_1", "v1") is not None
        assert service.peek("snap_1", "v1", variant="resilience") is not None

    def test_invalidating_a_snapshot_drops_every_variant(self):
        """
        Dropping the baseline while leaving a resilience assessment behind
        leaves the two describing different versions of the same network, which
        is worse than having neither.
        """
        from app.backend.services.analysis_store import AnalysisService

        service = AnalysisService()
        service.get("snap_2", "v1", lambda: {"kpis": {}})
        service.get("snap_2", "v1", lambda: {"rei": {}}, variant="resilience")
        service.invalidate("snap_2")
        assert service.peek("snap_2", "v1") is None
        assert service.peek("snap_2", "v1", variant="resilience") is None


class TestAChangedAnalysisShapeIsNotServedStale:
    """
    The cache key must distinguish a change to the DOCUMENT from a change to
    the network.

    `data_version` is a hash of facilities, products, demands and lanes. Adding
    a block to the serialised analysis changes none of them, so every entry
    written before the change stays valid by that key and is returned forever —
    the new code runs and no caller ever sees its output, on precisely the
    networks that have been analysed before.

    That is not hypothetical: `horizon.by_facility` came back empty on a
    network whose horizon had been solved correctly, because a document written
    before the field existed was what got served.
    """

    def test_bumping_the_analysis_version_invalidates_existing_entries(self):
        from app.backend.services import analysis_store
        from app.backend.services.analysis_store import AnalysisService

        service = AnalysisService()
        calls = []

        def compute():
            calls.append(1)
            return {"kpis": {}, "horizon": {"periods_modelled": 12}}

        service.get("snap_shape", "v1", compute)
        service.get("snap_shape", "v1", compute)
        assert len(calls) == 1, "the second read must come from the cache"

        original = analysis_store._ANALYSIS_VERSION
        try:
            analysis_store._ANALYSIS_VERSION = original + 1
            service.get("snap_shape", "v1", compute)
        finally:
            analysis_store._ANALYSIS_VERSION = original
        assert len(calls) == 2, (
            "a new document shape must recompute rather than serve a document "
            "written before the shape existed"
        )

    def test_the_version_does_not_collide_across_variants(self):
        from app.backend.services.analysis_store import AnalysisService

        service = AnalysisService()
        calls = []
        service.get("snap_var", "v1", lambda: calls.append("base") or {"kpis": {}})
        service.get("snap_var", "v1",
                    lambda: calls.append("rei") or {"rei": {}}, variant="resilience")
        assert calls == ["base", "rei"]
        assert service.peek("snap_var", "v1") is not None
        assert service.peek("snap_var", "v1", variant="resilience") is not None


class TestTheWebAppLoadsItsGatewayCredentials:
    """
    The assistant's language model was configured and unreachable, for the
    whole life of the server, because nothing in the web process read `.env`.

    `conftest.py`, `scripts/run_nlu_eval.py` and `netgravity/ingestion/config.py`
    each load it. `app/backend/app.py` did not — so ingestion reached the model
    and chat never did. `LLMGateway.available` was False, the orchestrator
    degraded to rule-based intent parsing and template reasoning exactly as
    designed, and the degradation was invisible precisely because that fallback
    is meant to be seamless.

    These pin the loading behaviour rather than the source line, so the guard
    survives a refactor of how it is loaded.
    """

    def test_a_value_in_the_file_reaches_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TEXT_API_TOKEN", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("TEXT_API_TOKEN=from-the-file\n", encoding="utf-8")

        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)
        assert os.environ.get("TEXT_API_TOKEN") == "from-the-file"

    def test_a_real_environment_variable_wins_over_the_file(self, tmp_path, monkeypatch):
        """
        override=False, so a deployment that sets the variable properly is
        never silently replaced by a checked-out file.
        """
        monkeypatch.setenv("TEXT_API_TOKEN", "from-the-environment")
        env_file = tmp_path / ".env"
        env_file.write_text("TEXT_API_TOKEN=from-the-file\n", encoding="utf-8")

        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)
        assert os.environ.get("TEXT_API_TOKEN") == "from-the-environment"

    def test_the_app_module_loads_credentials_at_import(self):
        """
        The behaviour that was missing: importing the web application must put
        the gateway credentials in the process environment when a .env exists.
        """
        import app.backend.app as app_module
        assert hasattr(app_module, "_load_gateway_credentials")

        repo_env = pathlib.Path(app_module.__file__).resolve().parents[2] / ".env"
        if not repo_env.exists():
            pytest.skip("no .env in this checkout; nothing to assert about loading it")
        assert os.environ.get("TEXT_API_TOKEN", "").strip(), (
            "importing the web app left TEXT_API_TOKEN unset despite a .env "
            "being present — the assistant would run without its model and say "
            "nothing about it"
        )

    def test_the_token_is_never_written_to_a_log_line(self):
        """
        The loader may report WHETHER a token is configured, never what it is.
        """
        import inspect
        import app.backend.app as app_module
        src = inspect.getsource(app_module._load_gateway_credentials)
        assert "bool(os.environ.get" in src
        # No formatting of the value itself into any string.
        assert 'os.environ.get("TEXT_API_TOKEN", "")}' not in src
        assert "%s\", os.environ" not in src
