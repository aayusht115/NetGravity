# Extraction / Parsing Agent

> **The data-ingestion pipeline is an implementation component of the
> Extraction / Parsing Agent, not a separate agent.**

That sentence is the architecture. Everything below follows from it.

---

## 1. Where it sits

```
CLIENT DATA ─────┐
                 │
EXTERNAL SOURCES ┤
                 ▼
      EXTRACTION / PARSING AGENT
                 │
      ┌──────────┴───────────┐
      ▼                      ▼
 ClientData             ExternalSignal
 Pipeline               extraction
 (netgravity/ingestion) (ExternalSignalAgent)
      │                      │
      └──────────┬───────────┘
                 ▼
         ExtractionResult          ← the agent stops here
                 │
                 ▼
           ORCHESTRATOR
                 │
      ┌──────────┼──────────┐
      ▼          ▼          ▼
    MILP       REI      Digital Twin
                 │
                 ▼
              P + REI → RF
                 │
                 ▼
            Reasoning → Grounding → Governance
```

## 2. What the agent is

[`extraction_agent.py`](../netgravity/orchestrator/agents/extraction_agent.py)
is roughly 350 lines over a 13,500-line pipeline. That ratio is the design, not
an accident of scheduling. The pipeline already does file discovery, parsing,
schema detection, column mapping, normalisation, row validation, entity
resolution, canonicalisation and provenance. Rebuilding any of it to make the
architecture "look like an agent" would have destroyed working code to satisfy a
diagram.

Four things the agent adds, none of which the pipeline had:

| # | Responsibility | Why it belongs here |
|---|---|---|
| 1 | **Routing** — which capability handles this source | Routing selects which deterministic pipeline runs, so it is decided from the filesystem, never by a model |
| 2 | **Acceptance** — `ACCEPTED` / `WARNING` / `HUMAN_REVIEW_REQUIRED` / `REJECTED` | One verdict over the pipeline's findings, so callers do not each invent their own threshold |
| 3 | **An orchestrator-facing contract** — `ExtractionResult` | The Orchestrator consumes a snapshot and evidence, never a workbook |
| 4 | **Snapshot registration** | Through the *existing* `SnapshotManager`, so fingerprinting, REI caching and staleness apply unchanged |

## 3. What it must never do

Compute. `ExtractionResult` has no field able to carry an REI, an RF, a
governance verdict or an optimisation objective — the same device
`ConversationalIntent` uses on the language side, for the same reason: evidence
that arrives pre-scored cannot be checked.

```python
for banned in ("rei", "rf", "risk_factor", "governance", "objective"):
    assert banned not in ExtractionResult.model_fields
```

The module imports no risk calculator, and a test strips its docstrings and
scans the remaining code to prove it.

## 4. The client-data path

```
Excel / CSV / client files
        ↓
ExtractionParsingAgent.extract()
        ↓
netgravity/ingestion  →  discover → parse → classify → map → normalise
                         → validate rows → resolve entities → build
        ↓
CanonicalNetwork            ← MAIN's own schema, not a copy
        ↓
SnapshotManager.register()  ← MAIN's own mechanism, not a copy
        ↓
Orchestrator → MILP → REI
```

**One canonical model.** `netgravity/ingestion/builder.py` imports
`netgravity.schemas.network` and constructs a `CanonicalNetwork` directly. There
is no `SourceCanonicalNetwork`, no adapter between two representations, and
therefore no way for them to drift. Asserted by identity, not by inspection:

```python
assert builder.CanonicalNetwork is main_schemas.CanonicalNetwork
```

**One snapshot mechanism.** Snapshot ids are `snap_` plus the first twelve
characters of `CanonicalNetwork.compute_data_version()`. Ingestion adds durable
content-addressed *persistence* under `data/curated/`; it does not add a second
registry. Because a network is a network whatever produced it, REI's material
fingerprint, cache keys and stale-evidence checks apply to ingested data with no
change to any of them.

## 5. The external-signal path

```
"There is a 70% probability of flooding around DC_DELHI."
        ↓
ExtractionParsingAgent (SourceType.EXTERNAL_SIGNAL_TEXT)
        ↓
ExternalSignalAgent      ← reused, not reimplemented
        ↓
ExternalSignal{event_type, location, severity, event_probability, basis}
        ↓
ORCHESTRATOR   ← pairs P with REI from the registry
        ↓
RF = P + REI − P·REI     ← the RF calculator owns this, not extraction
```

The agent stops at the signal. It does not look up REI and it does not compute
RF.

**Probability comes only from an explicit statement.** `"Catastrophic flooding
is expected"` yields `event_probability=None` and a `NO_EVENT_PROBABILITY`
warning; RF then reports `NOT_COMPUTABLE` rather than inferring a likelihood
from severity. Severity, confidence and probability stay three separate things.

> **The two signal types, now named apart.** The ingestion package's news and
> materiality signal was called `ExternalSignal` until Phase 4A, colliding with
> the orchestrator class above. It is now
> **`MarketIntelligenceSignal`** — bucket, direction, magnitude, and a
> qualitative confidence, used for forecast enrichment and root-cause context.
> It carries **no probability at all**, by design, and nothing converts one into
> the other. Deriving `P = 0.8` from `confidence: HIGH` would manufacture the
> single number that most directly drives RF and governance, out of a
> qualitative judgement that was never a likelihood.
>
> | | `MarketIntelligenceSignal` | `ExternalSignal` |
> |---|---|---|
> | Package | `netgravity.ingestion.schemas.signal` | `netgravity.orchestrator.schemas.requests` |
> | Subject | news / macro / policy / weather | a discrete hazard event |
> | Likelihood | none, deliberately | `event_probability` ∈ [0,1] or `None` |
> | Feeds | assumptions, root-cause narrative | `RF = P + REI − P·REI` |

## 6. Observed vs scenario

Everything this agent produces is **observed** state.

* `ExtractionRequest` has no field for a scenario or an override — checked by
  test against the field list.
* The agent's code contains no reference to `ScenarioIntentSpec` or
  `scenario_overrides` — checked by scanning the compiled source with
  docstrings stripped.
* Registered snapshots are not hypothetical.

There is no code path from ingestion to a scenario, which is the structural form
of "ingestion cannot silently mutate the baseline".

## 7. The AI boundary

The ingestion pipeline may use a model for ambiguous column mapping and document
interpretation. Three rules hold regardless:

1. **Off by default.** `ExtractionRequest.allow_ai` defaults to `False`, and with
   no key the pipeline runs rules-only. The entire test suite makes zero network
   calls.
2. **Never authoritative.** Anything a model proposes passes deterministic
   validation before it reaches a canonical snapshot. A model does not decide
   whether a facility exists.
3. **Stub is not assistance.** `provenance.ai_assisted` is true only when a
   *live* model produced part of the result. Canned stub output is counted
   separately, so demo output can never be read as a live extraction.

Confirmation is opt-in too: `auto_confirm_mappings` defaults to `False`, because
an unconfirmed mapping is exactly the case that should stop and ask.

## 8. Using it

```python
from netgravity.orchestrator.agents.extraction_agent import ExtractionParsingAgent
from netgravity.orchestrator.schemas.extraction import ExtractionRequest, SourceType

agent = ExtractionParsingAgent(snapshots=orchestrator.snapshots)

result = agent.extract(ExtractionRequest(
    source="data/mock/india",
    source_type=SourceType.CLIENT_DATA_DIRECTORY,
    register_snapshot=True,
))

if result.needs_review:
    show(result.review_items)          # a person decides
elif result.ok:
    network = result.canonical_data    # MAIN's CanonicalNetwork
    snapshot = result.snapshot_id      # already registered
```

Command line, unchanged from the source branch:

```bash
python scripts/generate_mock_dataset.py           # regenerate demo data
python -m netgravity.ingestion --source data/mock/india
```

## 9. Observability

An extraction emits `extraction.started` / `extraction.completed` /
`extraction.failed` through the standard logger, carrying `ingestion_id`,
`source_type`, `status`, `data_version`, `snapshot_id`, row counts and duration.
`ExtractionProvenance` records per-file counts, adapter, and whether each file
was AI-assisted or stubbed. Findings carry file, row, column and raw value.

No second audit architecture: the orchestrator's `AuditLogger` remains the
execution trail, and `netgravity/telemetry/` is a token/cost ledger for model
calls — complementary, not competing.

## 10. Known limitations

1. **Two LLM clients exist, and now share a contract rather than a transport**
   — `ingestion/ai/client.py` and `orchestrator/agents/llm_gateway.py`. Both
   are credential-gated and neither is authoritative.

   The gateway's *facts* — endpoints, limits, which errors are worth
   retrying, the vendor-URL misconfiguration guard — moved to
   `netgravity/llm/gateway_contract.py`, which both import. That is where the
   damage actually was: the gateway's budget is cumulative and shared across
   every holder of the token, so a limit corrected in one file and not the
   other did not cause a local bug, it spent someone else's capacity.

   The *transports* stay separate, deliberately. They use different HTTP
   libraries (ingestion is hand-rolled on stdlib `urllib` to add no
   dependency; the orchestrator uses `requests`, and its tests mock it), they
   raise different exceptions that are part of their callers' contracts, and
   the orchestrator's `LLMClient` Protocol is narrow on purpose — three
   members, no tool-invocation mechanism, a documented security boundary.
   Merging them would have to widen that wall to fit the ingestion client's
   richer surface.

   **One real defect was found and fixed while comparing them:** the
   orchestrator's gateway kept a private token counter and never recorded to
   `netgravity/telemetry/`. Two clients were spending one shared, cumulative
   budget into two ledgers that did not know about each other, so neither
   view was complete and "how much is left?" had no answer anywhere. It now
   records to the shared ledger; the private counter is kept because
   `max_requests_per_execution` is enforced from it, and that guard must not
   depend on a module whose recording is best-effort by design.

   Ingestion also adopted that per-instance call cap, which only the
   orchestrator had. Ingestion is the side that batches — a folder of forty
   files makes forty-plus calls without anyone deciding to.
2. **Performance is measured only at small scale** — 1,632 rows in ~73 ms.
   That is not evidence about production files.
3. **Good-to-have datasets are partly unproven.** Contracts and SKU are
   exercised; WMS, packaging, pick-and-pack and inbound/outbound activity have
   schema support that no test drives.
