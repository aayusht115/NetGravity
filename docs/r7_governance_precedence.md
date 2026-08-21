# R7 / R7B Governance Precedence — Finalised

Scope: one policy correction. Not a governance redesign. Action tiers, the
action taxonomy, R1–R6 and R8–R12 are untouched.

---

## 1. The policy

For an action **A** that requires evidence **E** to justify autonomous execution:

```
E.state ∈ {UNAVAILABLE, FAILED, STALE, NOT_COMPUTABLE, GROUNDING_FAILED}
    ⟹  AUTO_ACTION is prohibited
```

The verdict falls to the next conservative tier the action's own classification
permits — `APPROVAL_REQUIRED`, or `HUMAN_ONLY` where a stricter rule already
applies.

**What this says:** autonomy cannot be justified.
**What it does not say:** that risk is high, or that REI/RF are zero.

Those are different findings and the decision keeps them apart:
`blocked_by_missing_evidence` distinguishes "we could not establish the facts"
from "we did, and they are alarming".

---

## 2. Root cause

`ActionClassifier.classify` evaluates rules in precedence order, each returning
immediately. R7 sat above the evidence rules:

```python
if action_type in ANALYTICAL_ACTIONS:
    triggered.append("R7_ANALYTICAL_ONLY")
    return decide(AUTO_ACTION, ...)          # ← returned here

if missing_critical:                          # R7B — unreachable for REPORT
    return decide(APPROVAL_REQUIRED, ...)
```

`ANALYTICAL_ACTIONS` contains `REPORT`, and every risk-assessment run classifies
as `REPORT`. So an external-risk workflow whose REI engine had failed produced:

```
REI FAILED → RF NOT_COMPUTABLE → governance AUTO_ACTION
```

The same workflow **with** evidence and RF ≥ 0.8 produced `HUMAN_ONLY` (R6, which
sits above R7). Losing the evidence therefore *relaxed* the verdict from
`HUMAN_ONLY` to `AUTO_ACTION` — the inversion the architecture exists to prevent.

Two smaller defects surfaced while tracing it:

- **Stale REI was invisible to governance.** `missing_evidence` only records
  steps that *failed*. A stale batch means `resilience.assess` succeeds and RF
  then refuses it, so nothing was recorded as missing and the run looked fully
  evidenced.
- **`Intent.EXPLANATION` mapped to `ActionType.NONE`.** Added in Phase 2 but
  never added to `_infer_action_type`, so explanation runs short-circuited at R0
  and never reached the evidence rules at all.

---

## 3. Before and after

| Scenario | Before | After |
|---|---|---|
| REPORT, evidence intact | `AUTO_ACTION` (R7) | `AUTO_ACTION` (R7) — unchanged |
| REPORT, REI unavailable | **`AUTO_ACTION`** (R7) | `APPROVAL_REQUIRED` (R7B) |
| REPORT, REI stale | **`AUTO_ACTION`** (R7) | `APPROVAL_REQUIRED` (R7B) |
| REPORT, grounding failed | **`AUTO_ACTION`** (R7) | `APPROVAL_REQUIRED` (R7C) |
| CREATE_SCENARIO, REI unavailable | `AUTO_ACTION` | `AUTO_ACTION` — unchanged |
| REROUTE_FLOW, REI unavailable | `APPROVAL_REQUIRED` (R7B) | unchanged |
| CLOSE_FACILITY, low REI | `HUMAN_ONLY` (R2) | unchanged |
| CLOSE_FACILITY, REI unavailable | `HUMAN_ONLY` (R2) | unchanged |

The structural rule still outranks the evidence rule. Routing a closure to
`APPROVAL_REQUIRED` would itself have been a relaxation.

---

## 4. The change

**R7 becomes a candidate rather than a terminal return.** The evidence
constraints get to speak first; the candidate settles only if they do not object.

```
R6   high combined risk               → HUMAN_ONLY
R7   analytical output                → AUTO *candidate*  ─┐
R7B  required risk evidence unresolved → APPROVAL_REQUIRED │ evidence
R7C  numeric grounding failed          → APPROVAL_REQUIRED │ constraints
R7   settlement of the candidate       → AUTO_ACTION     ◄─┘
R8   moderate risk                     → APPROVAL_REQUIRED
```

Settling the candidate *there* — rather than further down — preserves the
pre-existing precedence exactly: an analytical action still bypasses R8–R10, as
it always did.

### Files

| File | Change |
|---|---|
| `governance/action_classifier.py` | `EvidenceState` enum; `ACTIONS_REQUIRING_RISK_EVIDENCE`; R7 candidate/settlement split; `unresolved_evidence` parameter |
| `schemas/actions.py` | `governing_rule`, `blocked_by_missing_evidence`, `evidence_status` on `GovernanceDecision` |
| `core/orchestrator.py` | `_unresolved_risk_evidence()`; `EXPLANATION` → `REPORT`; richer governance event |

---

## 5. Why action-aware rather than a blanket override

A blanket "any missing evidence → escalate" would be simpler and wrong. It would
escalate a hypothetical scenario for lacking a measurement that could not
possibly make it unsafe, and it would penalise workflows that never needed the
evidence in the first place.

The question the rule actually asks is narrower:

> Would a human justify running this unattended by pointing at risk evidence?

If yes, that justification evaporates when the evidence does. If no, nothing
changes.

```python
ACTIONS_REQUIRING_RISK_EVIDENCE = {
    REPORT, REROUTE_FLOW, CHANGE_CAPACITY, OPEN_FACILITY, CLOSE_FACILITY,
}
# excluded: NONE (nothing proposed), CREATE_SCENARIO (hypothetical by construction)
```

`GovernancePolicy.actions_requiring_risk_evidence` overrides the set, so a
genuinely low-stakes action can be declared autonomous without resilience
evidence — **explicitly, in policy**, rather than by accident of rule ordering.
That is the whole difference between the old behaviour and the exemption.

### Three deliberate non-escalations

1. **`CREATE_SCENARIO`** — cannot touch observed state.
2. **`NO_EVENT_PROBABILITY` / `NO_INPUTS`** — both imply no event was asserted,
   so nothing is missing. When REI is *also* genuinely broken, its step has
   failed and `missing_evidence` already carries it; no protection is lost.
3. **A workflow that never planned an REI step** has no REI gap. Evidence is
   only "missing" when the workflow asked for it — which is why a plain
   network-state query keeps its autonomy without any special-casing.

---

## 6. Information delivery is not constrained

The rule constrains **action autonomy**, not **information delivery**. With REI
down, the external-risk run still completes, reasoning still runs, and the
narrative still states what it knows and names what it does not:

> "The following analyses did not complete and their values are UNKNOWN (not
> zero): resilience.assess (UNAVAILABLE)."

Only the autonomy tier changes. The report is produced and returned either way.

---

## 7. Output contract

```
action_tier                 : REPORT
decision                    : APPROVAL_REQUIRED
governing_rule              : R7B_MISSING_CRITICAL_EVIDENCE
evidence_status             : {"resilience.assess": "UNAVAILABLE"}
blocked_by_missing_evidence : true
reason                      : "Autonomous execution is not permitted because
                               required risk evidence is unavailable: ... This is
                               a statement about the EVIDENCE, not about the
                               risk: exposure and risk factor remain UNKNOWN
                               rather than zero, and no value has been
                               substituted for them."
```

A test asserts the reason never contains "risk is high" when the actual finding
is missing evidence, and that a genuinely high measured RF still says so.

---

## 8. Remaining policy ambiguities

Named rather than silently resolved:

1. **`REPORT` is one tier covering very different things** — a risk assessment
   and a routine status summary share an action type, so both are now
   evidence-dependent. Separating them needs a new action type, which §1 of the
   brief excludes. The policy override is the intended escape hatch.
2. **`CHANGE_CAPACITY` is classified neither structural nor operational**, so it
   falls to R12's conservative default. Untouched here; worth an explicit
   decision.
3. **`NODE_MAPPING_UNAVAILABLE` is treated as an evidence failure.** An event was
   asserted and could not be tied to the network, which is a failure to
   establish evidence. Arguable as a data-quality issue instead; the conservative
   reading was chosen.
