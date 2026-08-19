# NetGravity — Data Ingestion Business Rules

**Audience:** anyone who needs to answer "why did the system do that?" without reading Python.
**Scope:** `netgravity/ingestion/` only. The optimisation engine's own rules are in `docs/mathematical_model.md`.
**Status:** guardrail thresholds are PROPOSED and awaiting team confirmation. Everything else is implemented and tested.

---

## 1. The one principle everything follows

> **Deterministic logic calculates. AI reads, maps and explains. AI never produces a number that reaches the optimizer.**

The model infers that a column called `Wt (kgs)` means kilograms and proposes a conversion factor. The multiplication itself is plain Python. The model reads a contract clause saying "add Rs. 5.00 per kg" and extracts `5.0`. The addition is plain Python.

This is testable, not aspirational — see §8.

---

## 2. Row-level validation rules

Two severities, and the distinction carries real weight:

| Severity | Meaning | Effect on the row |
|---|---|---|
| **ERROR** | The row cannot be used | Dropped from the network |
| **WARNING** | Suspicious, or repaired | **Kept**, and reported |

### Why we prefer repairing over rejecting

Dropping a row is not the safe default. If a demand row is dropped, that market vanishes from the network and the optimizer returns a confident, clean "OPTIMAL" answer **to the wrong problem**. A wrong answer that looks right is more dangerous than a loud warning.

So the rule is: **reject only what cannot be interpreted at all; repair anything whose intent is unambiguous, and say so loudly.**

### The rules

| Code | Rule | Severity | Rationale |
|---|---|---|---|
| R-001 | Required field missing or blank | ERROR | Cannot construct the record |
| R-002 | Value is not numeric | ERROR | Cannot be interpreted |
| R-003 | Negative value where only ≥ 0 is meaningful | ERROR | Negative cost/capacity/demand is not a real quantity |
| R-004 | Latitude/longitude outside global valid range | ERROR | Physically impossible |
| R-004 | Coordinates outside the configured geography | WARNING | Probably wrong, but the network may have expanded |
| R-005 | Unknown enum value (role, status, mode) | ERROR | Not a category the model understands |
| R-006 | Referenced ID does not exist | ERROR | A lane to a non-existent market cannot be solved |
| R-007 | Duplicate primary key | ERROR | Ambiguous which record is authoritative |
| R-008 | Origin and destination are the same node | ERROR | Not a transport lane |
| R-008 | Distance beyond plausible domestic range | WARNING | Likely a unit error; may be legitimate |
| R-008 | Zero transit time on a lane over 200 km | WARNING | **Would make every SLA constraint pass automatically** |
| R-009 | `service_level` in (1, 100] | WARNING — **repaired** | `95` unambiguously means 95%; divide by 100 |
| R-009 | `service_level` above 100 | ERROR | No sensible reading |
| R-011 | Observed throughput exceeds stated capacity | WARNING | One of the two figures is wrong; we cannot tell which |
| R-013 | File unreadable / unsupported type | WARNING | Skip the file, continue the run |
| R-014 | Contract contains a conditional surcharge | WARNING | The headline rate understates true cost — see §5 |
| R-015 | Low-confidence contract extraction | WARNING | Verify against the source document |
| R-016 | Unit conversion could not be applied | WARNING | Value kept unconverted; needs review |
| R-017 | AI column mapping below 90% confidence | WARNING | Needs human confirmation before being trusted |
| R-018 | Column could not be mapped | INFO | Dropped rather than guessed |
| R-019 | Zone-sheet demand with several products and no `Product_ID` | ERROR | Splitting a zone total across products would be a guess that changes the optimum |
| R-020 | A per-period column names its period (`Daily_Demand_Units`, `Monthly_Capacity`) | INFO | Converted to MONTH; the factor is recorded |
| R-021 | Demand exceeds capacity by roughly 30× / 12× / 7× | ERROR | Almost certainly a period mismatch, not a real shortfall — solving would report a **false INFEASIBLE** |
| R-021 | Demand exceeds capacity by some other ratio | WARNING | May be a genuine shortfall; worth confirming the periods match |
| R-022 | `Capacity_Per_Trip` without `Transit_Frequency` | WARNING | Lane left uncapacitated — a wrong cap invents a constraint, no cap merely loses one |

### Time periods — everything lands on MONTH

The engine works in months (`OptimizationConfig.cost_period` defaults to
`MONTH`, `days_per_period` to 30). Every per-period quantity is converted on
the way in, using the period stated in the **client's own column name**:

| Column says | Example | Treated as |
|---|---|---|
| a period | `Daily_Demand_Units`, `Monthly_Capacity` | converted to MONTH (×30, ×1) |
| nothing | `Capacity_Units`, `quantity` | assumed already MONTH |

Two details that are easy to get wrong:

- **Standard deviation is read from its own column**, not inherited from the
  quantity column. The workbook mixes periods on one sheet — `Daily_Demand_Units`
  sits beside a `Demand_Variability` example of "200 units/month". Inheriting
  would inflate variability ~5.5× and, through safety stock, inventory cost.
  Where variability genuinely is daily, it scales by √30, not 30.
- **Period-qualified names still resolve.** `Monthly_Capacity_Units` matches
  `Capacity_Units`; the period word is stripped for matching. Without this,
  stating the period explicitly would make a column silently unrecognised.

Because `Capacity_Units` states no period, R-021 exists as a numerical backstop:
demand exceeding capacity by close to 30× is treated as a unit error rather than
a real shortfall.

### Tolerances (deliberate)

Values like `Rs.1,20,000`, `₹4,200` and `1,20,000` are parsed correctly. Human-maintained spreadsheets contain currency symbols and thousands separators, and rejecting them would generate noise rather than insight.

---

## 3. What happens to a flagged item

1. It appears in the run summary count (`⚠ 3 flagged`)
2. It is listed individually with **file name, row number and column**, so it can be corrected at source
3. **The row still flows into the network** — flagging never silently removes data
4. For AI mappings, the model's own reasoning is printed alongside

There is currently **no UI**. All output is terminal text. The report is a structured object (`IngestionReport`) designed so a future screen can render the same information, but that screen does not exist yet.

---

## 4. AI column mapping (distributor files)

### The problem
Every distributor sends a different spreadsheet shape — different headers, different order, different units, dates in whatever format their ERP exports.

### The rules

| Rule | Behaviour |
|---|---|
| Confidence ≥ 0.90 | Accepted automatically |
| Confidence < 0.90 | Flagged (R-017) for human confirmation |
| Column not confidently mappable | Left **unmapped and dropped** (R-018), never guessed |
| Target field not in the canonical schema | Rejected outright — the model cannot invent fields |
| Unit conversion | Model proposes the factor; **arithmetic is deterministic code** |

### The confirmation loop (human-in-the-loop)

```bash
python -m netgravity.ingestion --list-mappings              # review what was proposed
python -m netgravity.ingestion --confirm-mapping <id>       # a human approves
```

Once confirmed, the mapping is cached and **every later file from that distributor skips the AI call entirely**. The AI cost is paid once per FORMAT, not once per file.

### Known limitation

Confidence is **self-reported by the model**, not statistically calibrated. It is a useful triage signal for routing work to a human — it is not a probability. Treat 0.90 as a review threshold, not a correctness guarantee.

---

## 5. Contract extraction

### The business case
Vendor A quotes Rs.10/kg. Vendor B quotes Rs.12/kg. A looks cheaper — until a clause in an annexure adds Rs.5/kg for "non-serviceable locations", which are exactly the remote destinations that matter.

### The rules

| Rule | Behaviour |
|---|---|
| Surcharge with **no** location list | Blanket — applies to every lane |
| Surcharge **with** a location list | Conditional — applies only where named |
| Any conditional surcharge present | Contract flagged `has_hidden_cost` → R-014 warning |
| Contracted rate | **Never overwritten** |
| Effective rate | Computed separately, at assembly time |

### Why the rate is never overwritten

Both numbers must stay visible. Overwriting the base rate would hide precisely the thing we are trying to surface.

### Worked example (from the shipped sample data)

| Destination | TransCorp (Rs.10 headline) | SpeedFreight (Rs.12 flat) | Cheaper |
|---|---|---|---|
| MKT_DELHI | 10 + 2 fuel = **12** | **12** | tie |
| MKT_GUWAHATI | 10 + 2 fuel + 5 NSL = **17** | **12** | **SpeedFreight** |

The lower headline rate is 42% more expensive at an NSL destination.

---

## 6. External signal guardrails

### The problem
External news can improve a forecast — or fill it with noise. The team's direction: *"Competitor news is low value. Carriers, logistics providers and suppliers are what actually move cost and service."*

### How a signal is classified
By **keyword matching** against trigger lists in `netgravity/ingestion/guardrails/thresholds.yaml`. This is deliberately deterministic, not model-driven: the guardrail is what protects the optimizer, so its decisions must be reproducible and reviewable.

### How a signal is scored

```
score = base_relevance (by bucket)
      + 0.25  if it names a facility/market we actually operate
      + 0.15 / +0.05 / −0.10   for HIGH / MEDIUM / LOW source confidence
      + 0.20  if it clears the materiality bar

passes if score >= that bucket's threshold
```

### The buckets (PROPOSED — awaiting team confirmation)

| Bucket | Base | Threshold | Outcome | Why |
|---|---|---|---|---|
| CARRIER | 0.70 | 0.60 | Passes | Capacity cuts and rate changes move cost and service directly |
| SUPPLIER | 0.70 | 0.60 | Passes | Outages propagate into inbound flow and safety stock |
| CUSTOMER | 0.70 | 0.60 | Passes | Expansion/contraction changes demand at specific zones |
| MACRO | 0.45 | 0.60 | Needs materiality ≥ 5% | Real, but small moves are noise |
| WEATHER | 0.65 | 0.60 | Passes, expires after 30 days | Genuine but short-lived |
| COMPETITOR | 0.10 | 0.95 | **Excluded by default** | Team judged it low signal-to-noise |
| UNKNOWN | 0.20 | 0.80 | Held back | Unclassifiable ≠ relevant |

### Two special rules

**Materiality (MACRO).** A move below 5% is logged, not surfaced. A move above it earns a `+0.20` bonus — because a network-wide signal like a fuel price rise affects every lane but can name no individual facility, so it can never earn the entity-match bonus. Without this, a major fuel shock would score *lower* than a trivial site-specific one.

**Expiry (WEATHER).** A signal older than 30 days is filtered. A cyclone from January must not remain an active assumption in August.

### Auditability — the non-negotiable

**Filtered signals are never deleted.** Every signal is stored with a verdict recording its bucket, score, threshold and a written reason. A silent filter is indistinguishable from a broken one.

### Current limitation
Signals are read from a seeded JSON file. There is **no live news feed** wired up. The structure and the filter are real; the source is manual.

---

## 7. Versioning

Every assembled network is saved as JSON, named with a SHA-256 hash of its own contents:

```
data/curated/4a7dcfef616aee27.json
```

- Same inputs → same version id (re-runs overwrite, never duplicate)
- Any input change → new version id
- So any KPI can be traced to the exact data that produced it

A `_manifest.json` indexes every version with a timestamp and label.

---

## 8. Is any of this industry-specific?

**No.** The split, honestly:

**Generic — works for pharma, FMCG, automotive, retail, anything**
- The four-path architecture (clean exports / messy files / documents / external signals)
- Every row-level validation rule in §2
- Storage, versioning, snapshots, reporting
- AI column mapping, contract extraction, confidence flagging
- The guardrail mechanism (bucket → score → threshold → audit)

**Configuration, not code — changeable without touching Python**
- Guardrail buckets, thresholds, trigger keywords → `thresholds.yaml`
- Currency, units, products → data files

**Geography-specific — three narrow items**
1. A coordinate range that **warns** (never rejects) if a facility falls outside India. One constant, one file.
2. The sample dataset (Baddi, Delhi NCR, Rs. pricing) — demo data, not pipeline logic.
3. Some trigger keywords ("monsoon", "GST", "PPAC") — in the YAML.

Nothing assumes consumer durables, India, or even physical goods. Pointed at a European pharma network with different CSVs, the pipeline runs unchanged; you would widen one coordinate constant and edit some keywords.

---

## 8b. Client field names (the workbook contract)

`NetGravity_Input_Data_Fields.xlsx` is the specification handed to a client.
Its field names are the **official input contract** — data arriving in exactly
that format loads without modification.

The engine's internal schema uses different names. `netgravity/ingestion/field_aliases.py`
translates between them:

| Workbook (client sends) | Internal (engine uses) |
|---|---|
| `Facility_ID` | `id` |
| `Type` | `role` |
| `Capacity_Units` | `capacity_units_per_period` |
| `Fixed_Annual_Cost` | `fixed_cost_per_year` |
| `Variable_Handling_Cost_Per_Unit` | `handling_cost_per_unit` |
| `Mandatory_Open_Flag` | `is_mandatory` |
| `Observed_Throughput_Units` | `observed_throughput` |
| `Zone_ID` / `Zone_Name` | `market_id` / `market_name` |
| `Daily_Demand_Units` | `quantity` |
| `SLA_Requirement` | `sla_days` |
| `Unit_Cost` | `rate_per_unit` |
| `Current_Lane_Flag` | `is_active_baseline` |
| `Weight` | `weight_kg` |

Rules:
- Matching is case- and separator-insensitive: `Facility_ID`, `facility id` and
  `FACILITY-ID` all resolve.
- Internal names also work, so existing files keep loading.
- Unrecognised columns are preserved, not dropped.
- **Demand may arrive on the Demand Zones sheet** (as the workbook defines it)
  rather than as a separate table. Both are supported; a dedicated demand file
  takes precedence when present. With more than one product in the catalogue a
  `Product_ID` column becomes mandatory, because splitting a zone total across
  products would be a guess that silently changes the optimum.

When the workbook changes, update `field_aliases.py` — not the parsers.

---

## 9. Where each rule lives

| Rules | File |
|---|---|
| Row-level checks (R-001…R-011) | `netgravity/ingestion/validation/row_checks.py` |
| Per-file parsing and repair | `netgravity/ingestion/adapters/structured.py` |
| Guardrail policy (editable) | `netgravity/ingestion/guardrails/thresholds.yaml` |
| Guardrail scoring | `netgravity/ingestion/guardrails/relevance.py` |
| Surcharge arithmetic | `netgravity/ingestion/schemas/contract.py` |
| Mapping confidence rules | `netgravity/ingestion/schemas/mapping.py` |
| Versioning | `netgravity/ingestion/snapshot.py` |
| Client field-name aliases | `netgravity/ingestion/field_aliases.py` |

Every rule above is covered by a test in `netgravity/ingestion/tests/`. Test names read as specifications — `pytest netgravity/ingestion/tests --collect-only -q` lists them.

---

## 10. Open items

| Item | Owner | Blocking? |
|---|---|---|
| Confirm guardrail thresholds in §6 | Team member owning guardrail definition | No — defaults work |
| Confirm LLM provider and supply API key | Team | No — stub mode runs everything |
| Live external news feed | Unassigned | No — seeded signals work |
| Ingestion console UI for mapping confirmation | Deferred (Phase 6) | No — CLI covers it |
| **Capacity period is unstated in the workbook.** `Capacity_Units` is described as "units/day or units/year" with a "units/month" example — three periods in one definition. Ingestion converts whatever the column NAME states and assumes MONTH when it states nothing; R-021 catches the mismatch numerically as a backstop. The workbook should state one period explicitly. | Team + mentor | Partly mitigated — R-021 blocks the silent failure, but the spec is still ambiguous |
