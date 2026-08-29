"""
Orchestrator — Authoritative KPI / Metric layer (Phase 9.1).

Consolidates access to metrics that already exist across `netgravity/metrics/`,
`netgravity/schemas/results.py`, `netgravity/orchestrator/schemas/risk.py`,
`netgravity/forecasting/schemas.py` and the Digital Twin, behind one typed,
traceable envelope (`KPIResult`, in `orchestrator/schemas/kpi.py`).

This package computes NOTHING an existing engine already computes. It reads
authoritative results already sitting on `ExecutionContext` and either:

  1. wraps a value verbatim (fill rate, REI, RF, forecast accuracy), or
  2. performs a genuinely NEW, legitimate DERIVED calculation using only
     already-authoritative inputs — documented per-function in `registry.py`
     and `scenario.py` — such as a scenario-vs-baseline delta for a metric the
     Digital Twin does not currently diff (RF, REI).

See `docs/authoritative_kpi_architecture.md` for the full design and the audit
that justified each decision.
"""
