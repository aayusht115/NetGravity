"""
Orchestrator — Reasoning agent.

Synthesises deterministic outputs into an explanation. It EXPLAINS; it does not
compute.

Three guarantees:

1. **Read-only.** It receives a structured, already-computed payload and cannot
   modify any value in it.
2. **Validated.** Output is checked before it reaches a caller. In particular,
   numbers it cites are cross-checked against the deterministic payload, and
   contradictions are flagged rather than passed through.
3. **Always available.** When the gateway is absent or fails, a deterministic
   template produces the narrative from the same figures. `source` records
   which path ran, so nobody has to guess.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from netgravity.llm.gateway_contract import MAX_OUTPUT_TOKENS
from netgravity.orchestrator.agents.llm_gateway import LLMGateway, extract_json
from netgravity.orchestrator.exceptions import LLMFailureError
from netgravity.orchestrator.reasoning.evidence import (
    build_evidence_pack,
    with_policy_thresholds,
)
from netgravity.orchestrator.reasoning.runtime import ReasoningRuntime
from netgravity.orchestrator.reasoning.validation import validate_reasoning_draft
from netgravity.orchestrator.reasoning.strategic_actions import format_pct
from netgravity.orchestrator.schemas.reasoning import (
    EvidenceCompleteness,
    ExecutiveBriefing,
    InsightSeverity,
    KPIInsight,
    MissingInformation,
    ReasoningDraft,
    ReasoningEvidencePack,
    ReasoningScope,
)
from netgravity.orchestrator.schemas.risk import ReasoningResult
from netgravity.orchestrator.validation.numeric_grounding import (
    ground_narrative,
    strip_ungrounded_claims,
)

logger = logging.getLogger(__name__)

_VALID_CONFIDENCE = {"LOW", "MEDIUM", "HIGH"}



#: Matches `ReasoningResult.narrative`'s own limit. Kept next to the assembly
#: that has to respect it rather than discovered when validation rejects it.
_MAX_NARRATIVE_CHARS = 700


def _bounded(sentences, limit: int) -> str:
    """
    Join sentences, stopping before `limit` and saying so if any were dropped.

    Sentence-wise rather than mid-word: half a figure is worse than no figure.
    """
    kept, used = [], 0
    for sentence in sentences:
        addition = len(sentence) + (1 if kept else 0)
        if used + addition > limit:
            break
        kept.append(sentence)
        used += addition
    text = " ".join(kept)
    dropped = len(sentences) - len(kept)
    if dropped:
        marker = f" (+{dropped} more)"
        if len(text) + len(marker) <= limit:
            text += marker
        else:
            text = text[:limit - len(marker)].rstrip() + marker
    return text


def _period_span(state: dict) -> str:
    """
    How to qualify a cost figure taken from `state`.

    Returns `" per period"` for a single-period solve — the phrasing every
    narrative used unconditionally — and, for a horizon, the number of periods
    the figure covers plus the per-period equivalent, which is the reading a
    planner compares against a monthly budget.

    The per-period figure is READ from the state, never computed here. Dividing
    a cost in a narrative would make this a second cost engine, and it would
    disagree with the first the moment either changed.
    """
    periods = state.get("periods_modelled")
    if not isinstance(periods, int) or periods <= 1:
        return " per period"
    per_period = state.get("cost_per_period")
    if isinstance(per_period, (int, float)):
        # Through `_money`, like every other amount in this file. It used to be
        # `f"{per_period:,.2f}"`, so a sentence read "…₹1,807,532 across the 12
        # periods modelled (150,627.70 per period)" — the same quantity twice
        # in one breath, once with a symbol and no cents and once with cents
        # and no symbol.
        return (f" across the {periods} periods modelled "
                f"({_money(per_period, state)} per period)")
    return f" across the {periods} periods modelled"


#: The share of total facility spend at which one site is a finding rather than
#: a row in a table. Two fifths: with four sites an even split is 25%, so this
#: is comfortably above "the largest of several" and below "almost all of it".
_SPEND_CONCENTRATION_SHARE = 0.40


def _money(value: Any, state: Dict[str, Any]) -> str:
    """
    An amount, in the currency this network is priced in.

    The template path printed `f"{value:,.2f}"`, so the prose read "business
    network cost at 150,627.70" beside an evidence chip reading ₹150,627.70 —
    the same figure twice on one card, once with its unit and once without.
    On a network priced in USD it was worse: a bare quantity in no unit,
    which a reader has no way to interpret and no reason to trust.

    `format_money` is the evidence layer's own, so the sentence and the chip
    beside it are formatted by one function. Where the upload named no
    currency it prints the amount bare — the honest rendering of an unknown
    unit, and the same thing every other surface does with it.
    """
    from netgravity.orchestrator.reasoning.evidence import format_money

    currency = state.get("currency") if isinstance(state, dict) else None
    try:
        return format_money(float(value), currency)
    except (TypeError, ValueError):
        return str(value)


def _sites(n: Any) -> str:
    """"site" or "sites", for a count that may arrive as a float."""
    try:
        return "site" if abs(float(n) - 1.0) < 1e-9 else "sites"
    except (TypeError, ValueError):
        return "sites"


def _lead_cap(text: str) -> str:
    """Capitalise the first letter only - `str.capitalize()` lowercases the rest,
    which would turn "CO2 transport cost" into "Co2 transport cost"."""
    return (text[:1].upper() + text[1:]) if text else ""


class ReasoningAgent:
    """Produces narrative synthesis over deterministic evidence."""

    def __init__(
        self,
        gateway: Optional[LLMGateway] = None,
        runtime: Optional[ReasoningRuntime] = None,
    ) -> None:
        self.gateway = gateway
        self.runtime = runtime
        #: Why the most recent live response could not be parsed, if it could
        #: not. Diagnostic only; never read as reasoning content.
        self._last_parse_failure: str = ""

    def reason(
        self,
        payload: Dict[str, Any],
        *,
        unavailable_evidence: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, str]] = None,
        allow_llm: bool = True,
        scope: ReasoningScope = ReasoningScope.NETWORK,
        entity_id: Optional[str] = None,
        user_question: str = "",
        single_request: bool = False,
    ) -> ReasoningResult:
        """
        Explain a set of deterministic results.

        Args:
            payload:              Structured results (scenario, optimization,
                                  kpis, rei, risk, external_evidence,
                                  market_evidence). Read-only.
            unavailable_evidence: Capability → {status, reason} for evidence that
                                  was expected but is MISSING. Passed through so
                                  the narrative reports absence rather than
                                  implying a zero.
            provenance:           execution/snapshot/scenario ids, attached to
                                  every accepted numeric claim.
            allow_llm:            False forces the template path.
            single_request:       True forbids the agent runtime, whatever the
                                  environment selects. The runtime reaches the
                                  model once per metric it decides to cite —
                                  an agent loop, not a request — so a caller
                                  that must spend exactly one model request
                                  sets this and gets the gateway path, where
                                  the whole evidence pack travels in the
                                  prompt and `generate()` is called once.

        Returns:
            ReasoningResult. Never raises — reasoning is advisory, and its
            failure must not invalidate deterministic truth.
        """
        missing = dict(unavailable_evidence or {})
        # The configured thresholds this narrative is allowed to cite, added
        # once here so every caller's payload carries them — the evidence pack
        # and the numeric grounding both read this same object.
        payload = with_policy_thresholds(payload)
        evidence_pack = build_evidence_pack(
            payload,
            scope=scope,
            entity_id=entity_id,
            user_question=user_question,
            unavailable=missing,
            provenance=provenance,
        )

        # Preferred live path: one focused OpenAI Agent, typed output and only
        # read-only evidence tools. Runtime availability is explicit, so an
        # installed SDK alone can never trigger a paid call.
        #
        # SKIPPED ENTIRELY under `single_request`. This runtime is an agent
        # loop: its prompt instructs the model to call `get_evidence` before
        # citing each metric, so a briefing quoting six figures costs at least
        # seven model requests. A caller that promised one request cannot use
        # it, however the environment is configured.
        if (allow_llm and not single_request
                and self.runtime is not None and self.runtime.available):
            try:
                draft = self.runtime.run(evidence_pack)
                violations = validate_reasoning_draft(draft, evidence_pack)
                if violations:
                    fallback = self._template(
                        payload, missing, scope, entity_id, evidence_pack)
                    fallback.validation_warnings.append(
                        "Agent output failed the reasoning contract; deterministic "
                        f"template used ({'; '.join(violations)})."
                    )
                    fallback.unavailable_evidence = missing
                    return self._ground(fallback, payload, provenance)
                result = self._from_draft(draft, evidence_pack)
                result.unavailable_evidence = missing
                return self._ground(self._validate(result, payload), payload, provenance)
            except Exception as exc:  # noqa: BLE001 - advisory layer fails closed
                logger.warning("orchestrator.reasoning.agent_failed error=%s", type(exc).__name__)
                fallback = self._template(payload, missing, scope, entity_id, evidence_pack)
                fallback.validation_warnings.append(
                    "OpenAI Agents reasoning was unavailable; deterministic template used."
                )
                fallback.unavailable_evidence = missing
                return self._ground(fallback, payload, provenance)

        if not allow_llm or self.gateway is None or not self.gateway.available:
            result = self._template(payload, missing, scope, entity_id, evidence_pack)
            result.unavailable_evidence = missing
            # The template only ever states values taken from the payload, so
            # it is grounded by construction — but it is checked anyway, because
            # "trust me" is not a validation strategy.
            return self._ground(result, payload, provenance)

        try:
            result = self._llm(payload, missing, user_question,
                               scope=scope, entity_id=entity_id)
        except LLMFailureError as exc:
            logger.warning("orchestrator.reasoning.llm_failed code=%s", exc.code.value)
            fallback = self._template(payload, missing, scope, entity_id, evidence_pack)
            fallback.unavailable_evidence = missing
            fallback.validation_warnings.append(
                f"LLM reasoning unavailable ({exc.code.value}); deterministic template used."
            )
            return self._ground(fallback, payload, provenance)

        if result is None:
            fallback = self._template(payload, missing, scope, entity_id, evidence_pack)
            fallback.unavailable_evidence = missing
            detail = getattr(self, "_last_parse_failure", "")
            fallback.validation_warnings.append(
                "LLM reasoning output could not be parsed; deterministic "
                "template used."
                + (f" Cause: {detail}" if detail else "")
            )
            return self._ground(fallback, payload, provenance)

        result.unavailable_evidence = missing
        validated = self._validate(result, payload)
        return self._ground(validated, payload, provenance)

    # ------------------------------------------------------------------
    # Numeric grounding
    # ------------------------------------------------------------------

    def _ground(
        self,
        result: ReasoningResult,
        payload: Dict[str, Any],
        provenance: Optional[Dict[str, str]],
    ) -> ReasoningResult:
        """
        Check every numeric claim against authoritative deterministic values.

        On failure the offending figures are REPLACED in the narrative — not
        left standing with a warning attached, because a caller reading the
        summary would never see the warning. Confidence is downgraded and
        `grounding_status` is set so governance can withhold automation.
        """
        visible = f"{result.summary} {result.recommendation}"
        if result.briefing is not None:
            visible = f"{visible} {result.briefing.visible_text()}"
        report = ground_narrative(
            visible,
            payload,
            provenance=provenance,
            structured_claims=result.grounded_claims or None,
        )

        result.grounding_status = report.status
        result.grounded_claims = [c.to_dict() for c in report.claims
                                  if c.verdict.value != "IGNORED"]

        if report.failed:
            result.summary = strip_ungrounded_claims(result.summary, report)
            result.recommendation = strip_ungrounded_claims(result.recommendation, report)
            result.evidence = [
                strip_ungrounded_claims(e, report) for e in result.evidence
            ]
            if result.briefing is not None:
                briefing = result.briefing
                briefing.opening = strip_ungrounded_claims(briefing.opening, report)
                briefing.context = strip_ungrounded_claims(briefing.context, report)
                briefing.recommendation = strip_ungrounded_claims(
                    briefing.recommendation, report)
                briefing.limitation = strip_ungrounded_claims(briefing.limitation, report)
                briefing.key_drivers = [
                    strip_ungrounded_claims(item, report) for item in briefing.key_drivers
                ]
                for insight in briefing.kpi_insights:
                    insight.headline = strip_ungrounded_claims(insight.headline, report)
                    insight.narrative = strip_ungrounded_claims(insight.narrative, report)
            result.validation_warnings.extend(report.warnings())
            result.confidence = "LOW"
            # Name the claims, not just count them.
            #
            # "contradicted=4" is not a diagnostic: it says something is wrong
            # four times without saying what, and the detail was sitting in
            # `report.warnings()` unlogged. Tracking down four contradicted
            # claims on a client network meant instrumenting this line by hand.
            logger.warning(
                "orchestrator.reasoning.grounding_failed source=%s contradicted=%d "
                "unsupported=%d claims=%s",
                result.source, len(report.contradicted), len(report.unsupported),
                " | ".join(
                    f"{c.raw_text!r} vs {c.matched_fact}={c.matched_value}"
                    for c in (report.contradicted + report.unsupported)[:6]),
            )

        return result

    # ------------------------------------------------------------------
    # LLM path
    # ------------------------------------------------------------------

    @staticmethod
    def _from_draft(
        draft: ReasoningDraft,
        evidence_pack: ReasoningEvidencePack,
    ) -> ReasoningResult:
        briefing = ExecutiveBriefing.model_validate(
            draft.model_dump(exclude={"confidence", "evidence_refs"})
        )
        summary_parts = [briefing.opening, briefing.context]
        summary_parts.extend(item.narrative for item in briefing.kpi_insights)
        cited_refs = list(draft.evidence_refs)
        for insight in briefing.kpi_insights:
            cited_refs.extend(insight.metric_refs)
            cited_refs.extend(insight.comparison_refs)
            cited_refs.extend(insight.driver_refs)
        cited_refs = list(dict.fromkeys(cited_refs))
        evidence = [
            f"{evidence_pack.metrics[ref].label} = "
            f"{evidence_pack.metrics[ref].display_value}"
            for ref in cited_refs
            if ref in evidence_pack.metrics
        ]
        return ReasoningResult(
            summary=" ".join(item.strip() for item in summary_parts if item.strip()),
            key_drivers=list(briefing.key_drivers),
            risks=[briefing.limitation] if briefing.limitation else [],
            recommendation=briefing.recommendation,
            confidence=draft.confidence,
            evidence=evidence,
            briefing=briefing,
            source="openai_agents",
        )

    #: Rows of any one list the prompt shows in full. A briefing cites one or
    #: two facilities; a twenty-row list buys nothing and costs the model the
    #: budget it needs to answer.
    _EVIDENCE_LIST_ROWS = 8

    #: Blocks whose rows are cut harder than the rest, and how many are kept.
    #:
    #: Measured on a scenario run: the payload was 11,807 characters and `rei`
    #: alone was 5,704 of them — one row per facility, each carrying a full
    #: exposure decomposition. The model bills its deliberation to the same
    #: 2,000-token budget it writes with, so half a prompt of resilience rows
    #: is paid for out of the words the reader gets, and the reply was
    #: truncated mid-JSON often enough that the scenario card was routinely
    #: written by the template on a build with a working gateway.
    #:
    #: The KIND of evidence is kept — the block is still there, still says how
    #: many rows exist, and the briefing can still cite the most exposed site.
    #: What goes is the tail nothing cites.
    _NARROW_LIST_ROWS = {"rei": 3, "facilities": 5, "warehouse": 5}

    #: Characters of evidence the prompt carries. Measured: the demo network's
    #: payload is ~12k and answers; the Canadian network's was ~40k and
    #: returned nothing twice. The bound is structural (see
    #: `_bounded_evidence`) — this is the last resort, and a slice at this size
    #: only happens on a payload the structural trim could not bring down.
    _EVIDENCE_CHARS = 16_000

    @staticmethod
    def _readable_figures(node: Any, currency: Optional[str],
                          key: str = "") -> Any:
        """
        The same payload with its money and percentages already rendered.

        A model told to copy a figure exactly will copy `150627.7036`, because
        that is what the JSON says. Rendering them here means "copy it exactly"
        and "write it the way a reader reads it" stop being two instructions
        that contradict each other — and it costs nothing from the output
        budget, which every extra line of prompt does.

        `_display` is the evidence layer's own renderer: the one behind the
        chips on the cards. Using it here is what makes a figure in a sentence
        and the same figure on a screen agree.
        """
        from netgravity.orchestrator.reasoning.evidence import _display

        if isinstance(node, dict):
            return {k: ReasoningAgent._readable_figures(v, currency, k)
                    for k, v in node.items()}
        if isinstance(node, list):
            return [ReasoningAgent._readable_figures(v, currency, key)
                    for v in node]
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            text, _unit = _display(node, key, currency)
            # `_display` returns the raw string for anything it has no opinion
            # about. Leaving those as numbers keeps counts as counts.
            return text if text != str(node) else node
        return node

    @classmethod
    def _bounded_evidence(cls, payload: Dict[str, Any]) -> str:
        """
        The payload as the model sees it: complete in shape, bounded in size.

        Three reductions, none of which removes a KIND of evidence:

          * the solved state is serialised once. `synthesise` writes it under
            both `scenario` and `optimization` because the template reads both
            names; the second copy becomes a pointer;
          * a list longer than `_EVIDENCE_LIST_ROWS` is cut to that many, with
            a sibling entry stating how many were left out — so the model can
            say "of twenty sites" without being handed twenty;
          * compact separators. `indent=1` spent a newline and a space on every
            leaf, for a reader that is not a person.

        The deterministic template reads the ORIGINAL payload and is unaffected.
        """
        def trim_value(value: Any, limit: int) -> Any:
            if isinstance(value, dict):
                return {k: trim_value(v, limit) for k, v in value.items()}
            if isinstance(value, list):
                if len(value) <= limit:
                    return [trim_value(v, limit) for v in value]
                kept = [trim_value(v, limit) for v in value[:limit]]
                # Stated, not silently dropped: a narrative that says "three
                # sites" about a network of twenty is worse than one that knows
                # it was shown eight rows of twenty.
                kept.append(
                    f"...{len(value) - limit} more rows not shown here; "
                    f"{len(value)} in total")
                return kept
            return value

        seen_state = None
        bounded: Dict[str, Any] = {}
        for key in sorted(payload):
            value = payload[key]
            # The duplicated solved state, by identity and then by content.
            if key in ("scenario", "network_state", "optimization"):
                if seen_state is None:
                    seen_state = (key, value)
                elif value is seen_state[1] or value == seen_state[1]:
                    bounded[key] = f"same as '{seen_state[0]}' above"
                    continue
            bounded[key] = trim_value(
                value, cls._NARROW_LIST_ROWS.get(key, cls._EVIDENCE_LIST_ROWS))

        return json.dumps(bounded, default=str, sort_keys=True,
                          separators=(",", ":"))[: cls._EVIDENCE_CHARS]

    @staticmethod
    def _intervention_options(payload: Dict[str, Any]) -> str:
        """
        The ONE change these results justify, for the model to phrase.

        Not a shortlist. A three-option block asking the model to choose cost
        the entire output budget in deliberation — measured against the live
        gateway at output_tokens=1984 with zero characters emitted, against
        1,688 tokens and real prose for the same payload without it. Choosing
        is a decision task; this model bills its thinking to the same allowance
        it writes with.

        There was nothing to delegate in the first place. `build_actions` walks
        the solved per-site rows down a fixed ladder — reopen what is closed
        before expanding, expand before building, build only where a region has
        nothing left to reopen or expand — and the first rung it reaches IS the
        recommendation. The model's job is to say it in a sentence a leader
        would act on, which is rewriting, not deciding.

        Returns "" when there are no facility rows: a recommendation the
        results do not support is not improved by having a model write it.
        """
        rows = payload.get("facilities")
        if not isinstance(rows, list) or not rows:
            return ""
        try:
            from netgravity.orchestrator.reasoning.strategic_actions import (
                build_actions,
            )
            state = payload.get("network_state") or {}
            unserved = state.get("unserved_demand")
            actions = build_actions(
                rows,
                unserved_demand=(unserved
                                 if isinstance(unserved, (int, float)) else None),
                limit=1,
            )
        except Exception:  # noqa: BLE001 — a briefing must not fail on this
            return ""
        if not actions:
            return ""
        if actions[0].key == "NO_ACTION":
            # "Nothing needs doing" is an answer the ladder has already
            # reached. Told to phrase an intervention anyway, the model would
            # manufacture one.
            return "\nRECOMMEND NO NETWORK CHANGE; say so plainly.\n"
        return f"\nRECOMMEND EXACTLY THIS: {actions[0].label}\n"

    def _llm(
        self, payload: Dict[str, Any], missing: Dict[str, Any],
        user_question: str = "",
        scope: ReasoningScope = ReasoningScope.NETWORK,
        entity_id: Optional[str] = None,
    ) -> Optional[ReasoningResult]:
        assert self.gateway is not None
        # Bound the payload. The gateway caps prompts at 100k characters, but
        # that is not the binding constraint: the backing model bills its
        # internal reasoning to a 2,000-token OUTPUT budget, and how much it
        # deliberates scales with how much it is given. See
        # `_bounded_evidence` for what was measured.
        evidence = self._bounded_evidence(payload)
        question = (user_question or "").strip()[:400]
        answering = bool(question) and not payload.get("kpi_chart")
        if answering:
            # See `_readable_figures`. Only this path, because it is the only
            # one that both writes figures and has nothing printed beside it.
            from netgravity.orchestrator.reasoning.evidence import _find_currency

            evidence = json.dumps(
                self._readable_figures(
                    json.loads(evidence), _find_currency(payload)),
                separators=(",", ":"), default=str)[:self._EVIDENCE_CHARS]

        missing_block = ""
        if missing:
            missing_json = json.dumps(missing, indent=1, default=str)[:4_000]
            missing_block = (
                "\nEVIDENCE THAT IS UNAVAILABLE (these analyses did NOT run — their "
                "values are UNKNOWN, not zero. Say so explicitly and never infer a "
                "value for them):\n"
                f"{missing_json}\n"
            )

        # Why this prompt is short, and why it asks for no verification.
        #
        # The gateway caps OUTPUT at MAX_OUTPUT_TOKENS and the backing model is
        # a reasoning model that bills its internal reasoning to that same
        # allowance. Measured against the live gateway, the previous prompt
        # returned output_tokens=1984 and ZERO characters of text on every
        # call: the model spent the entire budget deliberating and emitted
        # nothing, so the reasoning layer silently degraded to templates for
        # the whole of its existence.
        #
        # Three things bought the text back, measured one at a time:
        #   * dropping the `claims` array. Restating every figure with its
        #     exact value is a verification task, and it dominated the
        #     reasoning. `ground_narrative()` accepts `structured_claims=None`
        #     and falls back to `extract_numeric_claims()`, which reads the
        #     numbers out of the visible text — the same grounding, without
        #     asking the model to do it twice;
        #   * telling it NOT to verify or recompute. The old rules ("every
        #     number you write is checked", "any figure that does not match
        #     will be REMOVED") invited exactly the deliberation that consumed
        #     the budget. Grounding still happens, in code, afterwards;
        #   * capping each string in the schema itself rather than in prose.
        #
        # Result on the same evidence: 1,429 output tokens, 420 characters,
        # valid JSON. The fields are ordered by how much they matter, so a
        # longer-than-expected reply loses the least important first.
        # THE QUESTION, when there is one.
        #
        # The prompt used to say only "explain what these figures mean for the
        # business" — so it produced the same executive briefing regardless of
        # what had been asked. "Which distribution centre is most utilised?"
        # and "Why is demand unserved?" both returned the network's total cost
        # and fill rate. Every figure was correct and neither answered the
        # question, which is the most misleading shape a wrong answer can take.
        #
        # It is placed AFTER the evidence and immediately before the response
        # contract, because that is the position a model weights most heavily,
        # and it is bounded so a long paste cannot displace the instructions.
        if question:
            ask_block = (
                "\nTHE USER ASKED:\n"
                f"{question}\n\n"
                "Answer THAT question, directly, in the summary — first "
                "sentence, not the last. Use the figures above and no others. "
                "If the results do not contain what was asked for, say plainly "
                "that it is not available and report what the results DO show; "
                "never answer a different question instead.\n"
            )
        else:
            ask_block = (
                "\nNo specific question was asked, so explain what these "
                "results mean for the business.\n"
            )

        # THE INTERVENTIONS THIS NETWORK'S OWN RESULTS JUSTIFY.
        #
        # Derived, not invented: `build_actions` walks the solved per-site rows
        # down a fixed ladder — reopen what is closed before expanding, expand
        # before building, and only build where a region has nothing left to
        # reopen or expand. The model is given the result and told to phrase
        # one of them.
        #
        # Absent for a chart card (which describes and does not prescribe) and
        # for any payload with no facility rows, in which case the field falls
        # back to the open form it had. A recommendation the results do not
        # support is not improved by having a model write it.
        options_block = ""
        if not payload.get("kpi_chart"):
            options_block = self._intervention_options(payload)

        # ANSWERING A QUESTION IS THE OTHER CASE WHERE THE FIGURES ARE THE POINT.
        #
        # A dashboard card is read beside the numbers it describes, which is
        # why the default rule below forbids the model from writing any. The
        # chat assistant has nothing beside it — its sentence is the whole
        # answer — and under that rule "what is my total network cost?" came
        # back as "The total network cost is reported as the business network
        # cost." Grounded, true, and not an answer.
        #
        # Same safety as the chart exception: grounding runs afterwards and
        # removes any figure that is not in the results above.
        #
        # The rule below carries no currency or rounding instruction, because
        # `_readable_figures` has already rendered every amount in the
        # network's own currency and every percentage at the precision the
        # cards use. "Copy it exactly" therefore produces a well-formed figure
        # on its own — and the two clauses that would otherwise have said so
        # are two clauses not paid for out of the same 2,000-token allowance as
        # the answer itself.

        prompt = (
            "DETERMINISTIC RESULTS:\n"
            f"{evidence}\n\n"
            "You are a supply-chain analyst writing for logistics executives. "
            "The figures above are authoritative. Use only those numbers. Do "
            "not verify or recompute them — that is done for you afterwards. "
            "If something is absent, say it is not available rather than "
            "guessing.\n"
            f"{missing_block}"
            f"{options_block}"
            f"{ask_block}\n"
            # HOW TO WRITE. Every rule is here because its absence produced a
            # specific defect on screen, and every one is stated in as few
            # tokens as it can be — see the note above on the output budget.
            #
            #   no figures      the project's currency is applied afterwards,
            #                   in one place. A model-written amount appears
            #                   beside a table in another currency, and is
            #                   then stripped by grounding, leaving a sentence
            #                   with a hole in it;
            #   third person    the screens read as a system narrating itself
            #                   ("I see", "my models") rather than as a report;
            #   real names      "Warehouse A" is a placeholder, and the results
            #                   carry the actual site names;
            #   cost + service  a plan that costs less while stranding demand
            #                   is cheaper and not therefore better;
            #   once            the same finding as headline, paragraph and
            #                   recommendation reads as three findings;
            #   not a roster    "name the real things" is satisfied, literally
            #                   and uselessly, by listing every site in the
            #                   payload. A live call on a network where every
            #                   site read the same returned six facility names
            #                   as the conclusion AND as the paragraph under
            #                   it — a card that names its subject twice and
            #                   says nothing about it. A conclusion is a
            #                   statement; the names belong inside it.
            # ONE CHART is the exception to "no figures".
            #
            # The rest of this product applies the project's currency in one
            # place afterwards, so a model-written amount would arrive in the
            # wrong one. A chart explanation is read BESIDE the chart, where a
            # sentence with no quantities in it ("utilisation is high at two
            # sites") says less than the picture it sits under — so here the
            # figures are the point, and every value the model may use is
            # registered as a citable fact in `numeric_grounding._FACT_SPEC`
            # before it is sent. Anything it writes that is not one of those
            # is still removed by the validator afterwards.
            + ("RULES: use ONLY figures from the results above. Write them as "
               "a reader would — thousands separated, decimals rounded, "
               "'approximately' when rounded — never a field name or a raw "
               "key. No figure that is not there. Summary = the finding in one "
               "sentence, then TWO more that do not repeat it: the key figures, "
               "how they compare, what the pattern means to run. "
               if payload.get("kpi_chart") else
               # SHORT ON PURPOSE, and the length is the whole engineering
               # problem here. The backing model bills its internal reasoning
               # to the same 2,000-token output allowance as its text, so
               # every additional instruction is paid for in deliberation
               # before a character is emitted. A fuller version of this rule
               # — six clauses, about fifty words — was measured against the
               # live gateway at output_tokens=1984 with ZERO characters of
               # text on two calls out of three: the reasoning layer silently
               # degraded to templates, which is the exact failure the note
               # above this prompt records. Cut to one clause per defect.
               # "Never a field name" is the chart rule's clause, here for
               # the same reason: told to copy the evidence exactly, the model
               # copied the key with it — 'the most utilised, with
               # utilization_pct "77.1%"' — which reads as a database dump
               # rather than an answer.
               ("RULES: quote the figure asked for, exactly as written "
                "above, in sentence one. Never a field name or raw key. "
                "No figure that is not in the results. "
                if answering else
                "RULES: no figures, amounts, percentages or currency symbols. "))
            + (
            "Third person, never 'I' or 'my'. Plain business English, no "
            "solver or model vocabulary. Name the real things the results "
            "contain and never a placeholder; do not remark on the kinds "
            "they do not contain. No urgency the results do not establish. "
            "If cost improves but service or capacity does not, say both. "
            "Say each thing once. State a finding, not a roster: name at most "
            "two, or say 'no facility'/'every site' when it holds for all.\n"
            "Reply with ONLY this JSON, every string short:\n"
            # A CHART CARD ASKS FOR WHAT A CHART CARD SHOWS.
            #
            # It renders the summary and nothing else: the recommendation is
            # forced empty (a chart describes, it does not prescribe), and the
            # drivers and risks are not drawn at all. Asking for them anyway
            # spent output budget on three fields headed for the bin — and
            # this model answers by reasoning first, so a bigger object is
            # paid for in thinking before a single character is emitted. The
            # gateway's own diagnosis when it ran out was "shorten the prompt
            # or ask for a smaller object"; this is the second.
            + ('{"summary":"<the finding in 1 sentence, then 2 more that do '
               'not repeat it>",'
               '"evidence":["<figure copied from the results>"],'
               '"confidence":"LOW|MEDIUM|HIGH"}\n'
               if payload.get("kpi_chart") else
            '{"summary":"<conclusion in 1 sentence, then what it means in 1 '
            'more>",'
            + ('"recommendation":"<the RECOMMEND line above, as 1 sentence>",'
               if options_block else
               '"recommendation":"<1 sentence, one next step>",')
            + (
            '"confidence":"LOW|MEDIUM|HIGH",'
            '"key_drivers":["<6 words>","<6 words>"],'
            '"risks":["<the one thing not to miss, 12 words>"]}\n'))
            + "Set confidence to LOW if key results are missing or the network "
              "is infeasible; HIGH only when the results are complete.\n"
            )
        )

        response = self.gateway.generate(prompt, purpose="reasoning")
        parsed = extract_json(response.output)
        if not parsed:
            # WHY it could not be parsed is operationally different in each
            # case, and the generic message hid an output-cap problem for a
            # whole validation phase. Recorded on the agent for the caller to
            # attach; nothing is inferred from the unparseable text itself.
            self._last_parse_failure = self._describe_parse_failure(response)
            logger.warning(
                "orchestrator.reasoning.llm_unparseable request_id=%s reason=%s",
                response.request_id, self._last_parse_failure,
            )
            return None

        confidence = str(parsed.get("confidence", "LOW")).strip().upper()
        if confidence not in _VALID_CONFIDENCE:
            confidence = "LOW"

        def as_list(key: str) -> List[str]:
            raw = parsed.get(key, []) or []
            if isinstance(raw, str):
                raw = [raw]
            return [str(x)[:400] for x in raw if str(x).strip()][:8]

        structured = [c for c in (parsed.get("claims") or []) if isinstance(c, dict)][:20]

        summary = str(parsed.get("summary", ""))[:2000]
        drivers = as_list("key_drivers")
        risks = as_list("risks")
        # A CHART EXPLANATION MAKES NO RECOMMENDATION, on this path either.
        #
        # The template path already returns none; without this the model's
        # `recommendation` came straight through and a utilisation chart
        # advised shifting volume between sites — an instruction needing
        # closure economics and a second solve, printed under an observation
        # that supports nothing of the kind. Dropped here rather than removed
        # from the JSON contract, which is shared with the flows that do
        # legitimately recommend.
        recommendation = ("" if payload.get("kpi_chart")
                          else str(parsed.get("recommendation", ""))[:1000])

        return ReasoningResult(
            summary=summary,
            key_drivers=drivers,
            risks=risks,
            recommendation=recommendation,
            confidence=confidence,
            evidence=as_list("evidence"),
            # A BRIEFING, like every other path returns.
            #
            # This path used to return None here, and `/api/insights` reads
            # `result.briefing.kpi_insights` directly — so the moment a live
            # call succeeded, the Overview raised AttributeError and 500ed.
            # The failure was invisible while the LLM was off, because the
            # template path always builds one.
            briefing=self._briefing_from_parts(
                summary=summary, drivers=drivers, risks=risks,
                recommendation=recommendation,
                # WHAT this briefing is about. Without it every gateway-written
                # briefing came back NETWORK-scoped with no entity, including
                # on a scenario run — so a screen could not tell a what-if's
                # explanation from the network's, which is the exact confusion
                # passing `scope` through was meant to end.
                scope=scope, entity_id=entity_id),
            source="llm",
            grounded_claims=structured,
        )

    @staticmethod
    def _briefing_from_parts(*, summary: str, drivers: List[str],
                             risks: List[str], recommendation: str,
                             scope: ReasoningScope = ReasoningScope.NETWORK,
                             entity_id: Optional[str] = None) -> ExecutiveBriefing:
        """
        The gateway's one narrative, in the shape every consumer reads.

        One `KPIInsight`, not none: the gateway path returns a single summary
        rather than per-theme findings, and a briefing with an empty
        `kpi_insights` renders as a blank card on screens that iterate it.
        One insight carrying what the model actually said is the honest
        representation — inventing themes to fill the list would not be.

        The prompt asks for the conclusion in one sentence and what it means in
        one more, so the two are separated here rather than run together: the
        card leads with the conclusion, and a generic heading over it — "What
        these results show" — pushes the conclusion into the body and heads the
        card with a label instead.
        """
        from netgravity.orchestrator.reasoning.card import (
            first_sentence,
            rest_after_first_sentence,
        )

        #: The schema's own limit for `KPIInsight.headline`. A first sentence
        #: longer than this is not a headline, whatever it is.
        headline_limit = 140

        insights: List[KPIInsight] = []
        if summary:
            lead = first_sentence(summary)
            fits = len(lead) <= headline_limit
            insights.append(KPIInsight(
                theme="Summary",
                # Empty when the conclusion will not fit: the card then derives
                # a short lead from the narrative and shows the whole of it
                # underneath, rather than a sentence chopped at 140 characters
                # with its remainder nowhere.
                headline=lead if fits else "",
                narrative=((rest_after_first_sentence(summary) or summary)
                           if fits else summary)[:700],
            ))
        return ExecutiveBriefing(
            scope=scope,
            entity_id=entity_id,
            opening=summary[:500],
            key_drivers=drivers[:4],
            kpi_insights=insights,
            recommendation=recommendation[:350],
            limitation=(risks[0][:350] if risks else ""),
        )

    @staticmethod
    def _describe_parse_failure(response: Any) -> str:
        """
        Say why a successful gateway call produced no usable JSON.

        Three distinguishable causes, and they call for different responses:
        an exhausted output budget is a prompt-length problem, an empty body is
        a gateway problem, and anything else is the model not following the
        response contract. Guessing between them costs a live call each time.
        """
        usage = getattr(response, "usage", None)
        output_tokens = getattr(usage, "output_tokens", None) or 0
        text = (getattr(response, "output", "") or "")

        if not text.strip():
            # An empty body AFTER the budget was spent is not a gateway fault:
            # a reasoning model bills its internal reasoning to the same output
            # allowance, so it can consume the whole cap and emit nothing
            # visible. Naming that as "a gateway problem" sent a prompt-length
            # issue to the wrong place.
            if output_tokens >= MAX_OUTPUT_TOKENS * 0.95:
                return (
                    f"the model spent its entire {MAX_OUTPUT_TOKENS}-token output "
                    f"allowance on internal reasoning and emitted no visible text "
                    f"(output_tokens={output_tokens}). This is a response-length "
                    f"problem, not an unreachable gateway: shorten the prompt or "
                    f"ask for a smaller object."
                )
            return ("the gateway returned an empty body; no output to parse "
                    f"(output_tokens={output_tokens})")

        if output_tokens >= MAX_OUTPUT_TOKENS:
            return (
                f"the response exhausted the gateway's {MAX_OUTPUT_TOKENS}-token "
                f"output budget (output_tokens={output_tokens}), so the JSON was "
                f"truncated mid-structure. This is a response-length problem, "
                f"not malformed model behaviour: a reasoning model spends part "
                f"of that budget on internal reasoning before emitting text."
            )

        return (
            f"the response was not valid JSON and no JSON object could be "
            f"recovered from it (output_tokens={output_tokens}, "
            f"{len(text)} chars). First 200 characters, for diagnosis: "
            f"{text.strip()[:200]!r}"
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, result: ReasoningResult, payload: Dict[str, Any]) -> ReasoningResult:
        """
        Sanity-check model narrative against the deterministic payload.

        This cannot catch every hallucination, but it catches the ones that
        matter most: claiming feasibility when the solver said otherwise, and
        asserting confidence the evidence does not support.
        """
        warnings: List[str] = []

        if not result.summary.strip():
            warnings.append("Reasoning returned an empty summary.")

        infeasible = self._is_infeasible(payload)
        text = f"{result.summary} {result.recommendation}".lower()

        if infeasible:
            if re.search(r"\bfeasible\b", text) and "infeasible" not in text:
                warnings.append(
                    "Narrative asserts feasibility while the solver reported INFEASIBLE. "
                    "The solver is authoritative."
                )
            if result.confidence == "HIGH":
                result.confidence = "LOW"
                warnings.append(
                    "Confidence downgraded to LOW: the network is infeasible."
                )

        # Confidence must not exceed evidence completeness.
        if result.confidence == "HIGH" and not result.evidence:
            result.confidence = "MEDIUM"
            warnings.append("Confidence downgraded to MEDIUM: no evidence was cited.")

        result.validation_warnings.extend(warnings)
        if warnings:
            logger.warning("orchestrator.reasoning.validation warnings=%s", warnings)
        return result

    @staticmethod
    def _is_infeasible(payload: Dict[str, Any]) -> bool:
        for key in ("optimization", "scenario", "network_state"):
            block = payload.get(key)
            if isinstance(block, dict):
                status = str(block.get("solver_status", "")).upper()
                if "INFEASIBLE" in status:
                    return True
                if block.get("is_feasible") is False:
                    return True
        return False

    # ------------------------------------------------------------------
    # Deterministic template path
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Deterministic insight themes
    # ------------------------------------------------------------------
    #
    # One method per theme, each reading only figures the evidence pack already
    # holds. They exist as separate methods so a theme can be read, tested and
    # argued with on its own, and so adding one never touches the others.
    #
    # Three rules hold in every one of them:
    #
    #   1. An absent metric yields NO insight. Never a zero, never a hedge, and
    #      never a sentence built round a number that was not measured.
    #   2. Every figure stated is a value from the payload, formatted — not
    #      derived, not scaled, not combined into a new quantity. The numeric
    #      grounding check verifies every number in this prose against the
    #      authoritative evidence, and it is meant to pass by construction
    #      rather than by luck.
    #   3. A threshold is imported from the module that owns it, never restated
    #      here. `UTILIZATION_THRESHOLDS` already decides what over- and
    #      under-utilised mean for `NetworkKPIs.overutilized_count`; a second
    #      opinion about it in this file would be a second definition.

    @staticmethod
    def _service_insights(state: Dict[str, Any], refs_for) -> List[KPIInsight]:
        """Whether the network serves what it promised, and at what service level."""
        out: List[KPIInsight] = []
        unserved = state.get("unserved_demand")
        total = state.get("total_demand")
        fill = state.get("demand_fill_rate")
        sla_pct = state.get("pct_demand_in_sla")

        if isinstance(unserved, (int, float)) and unserved > 0:
            share = (f" of {total:,.0f} units of demand"
                     if isinstance(total, (int, float)) and total > 0 else "")
            out.append(KPIInsight(
                theme="Service",
                headline="This plan leaves demand the network has no way "
                         "to deliver",
                severity=InsightSeverity.RISK,
                narrative=(
                    f"I see {unserved:,.0f} units{share} left unserved. This is a "
                    f"capacity or reachability limit in the plan itself, not a "
                    f"rounding artefact — every unit of it is demand the current "
                    f"footprint has no way to deliver."
                ),
                metric_refs=refs_for("unserved_demand"),
                comparison_refs=refs_for("total_demand"),
            ))
        elif isinstance(fill, (int, float)):
            # As a percentage, which is how a fill rate is discussed.
            #
            # This used to print the stored ratio — "a demand fill rate of
            # 1.000" — on the reasoning that converting it would break the
            # grounding check. It does not: `_equivalent_values` in the
            # numeric validator exists for exactly this case and says so
            # ("a fill rate stored as 0.968 may be written 96.8%"). The ratio
            # was reaching the Insights page and the Overview tile as the
            # headline figure of the service finding, where "1.000" is the
            # storage format rather than an answer.
            out.append(KPIInsight(
                theme="Service",
                headline=("Every unit of stated demand is served by this "
                          "plan" if fill >= 1.0 else
                          "Part of the stated demand is not served by "
                          "this plan"),
                narrative=(
                    f"I see a demand fill rate of {fill * 100:,.1f}%. "
                    + ("Every unit of stated demand is served by this plan, so "
                       "service is not what constrains it."
                       if fill >= 1.0 else
                       "Part of the stated demand is not served by this plan.")
                ),
                metric_refs=refs_for("demand_fill_rate"),
            ))

        if isinstance(sla_pct, (int, float)) and sla_pct < 100.0:
            # What the OTHER (100 - sla_pct)% is depends entirely on how service
            # was enforced, and this insight used to assert one answer for every
            # engine: "the remainder is served, but not inside the lead time".
            #
            # Under TRANSIT_TIME_SLA_FEASIBILITY — the only methodology this
            # build implements — an SLA-infeasible lane is deleted from the arc
            # set before the solve. Nothing CAN be served late. The remainder is
            # demand that was not served at all, which is a different finding
            # requiring a different intervention: capacity or reachability, not
            # expediting. The engine's own `unserved_demand` said so on the very
            # same screen, so the product contradicted itself.
            # Every figure quoted below is one the evidence pack holds. The
            # obvious phrasing — "the other 31.52% is unserved" — computes a
            # percentage that appears in no authoritative result, and the
            # numeric grounding check rightly strips it. `unserved_demand` is
            # the authoritative statement of the same fact, so it is what the
            # sentence cites.
            methodology = str(state.get("service_methodology") or "")
            unserved = state.get("unserved_demand")
            unserved_clause = (
                f" The engine reports {unserved:,.0f} units of demand unserved."
                if isinstance(unserved, (int, float)) and unserved > 0 else ""
            )
            if methodology == "TRANSIT_TIME_SLA_FEASIBILITY":
                headline = ("Some demand cannot be reached inside its lead "
                            "time, so this plan does not serve it at all")
                narrative = (
                    f"I see {format_pct(sla_pct)} of demand served within its stated "
                    f"service level. The rest is not served late — it is not "
                    f"served at all: this plan moves volume only on lanes that "
                    f"already meet the destination's lead time, so demand it "
                    f"cannot reach in time is left unserved rather than "
                    f"delivered outside SLA." + unserved_clause
                )
            else:
                # An engine whose methodology this result does not record.
                # State the figure and stop, rather than inventing what the
                # rest of the demand did.
                headline = ("Some demand falls outside its stated service "
                            "level, and this run does not record why")
                narrative = (
                    f"I see {format_pct(sla_pct)} of demand served within its stated "
                    f"service level. How this run enforced service is not "
                    f"recorded on the result, so I cannot say whether the rest "
                    f"was delivered late or not delivered at all."
                    + unserved_clause
                )
            out.append(KPIInsight(
                theme="Service",
                headline=headline,
                severity=InsightSeverity.RISK,
                narrative=narrative,
                metric_refs=refs_for("pct_demand_in_sla"),
                comparison_refs=refs_for("unserved_demand"),
            ))
        return out

    @staticmethod
    def _utilization_insights(state: Dict[str, Any], payload: Dict[str, Any],
                              refs_for) -> List[KPIInsight]:
        """
        Where the capacity is tight and where it is idle.

        Both directions matter and they are different findings: a site above the
        over-utilisation threshold is a service risk this period, while a set of
        sites well below the under-utilisation threshold is money being spent on
        capacity nobody is using.
        """
        from netgravity.config.defaults import UTILIZATION_THRESHOLDS

        over_pct = UTILIZATION_THRESHOLDS["over_threshold"] * 100.0
        under_pct = UTILIZATION_THRESHOLDS["under_threshold"] * 100.0

        out: List[KPIInsight] = []
        avg_util = state.get("avg_utilization_pct")
        max_util = state.get("max_utilization_pct")

        facilities = [f for f in (payload.get("facilities") or [])
                      if isinstance(f, dict) and f.get("is_open")
                      and isinstance(f.get("utilization_pct"), (int, float))]
        over = sorted((f for f in facilities if f["utilization_pct"] >= over_pct),
                      key=lambda f: -f["utilization_pct"])
        under = sorted((f for f in facilities if f["utilization_pct"] <= under_pct),
                       key=lambda f: f["utilization_pct"])

        def name(f: Dict[str, Any]) -> str:
            return str(f.get("facility_name") or f.get("facility_id") or "a facility")

        # A scoped payload holds exactly one facility, and on that screen the
        # network average is the wrong subject: a reader looking at one DC needs
        # that DC's number, not a mean that includes six sites they did not ask
        # about.
        if len(facilities) == 1:
            only = facilities[0]
            util = only["utilization_pct"]
            if util >= over_pct:
                verdict = (f"That is at or above the {over_pct:.0f}% threshold, so "
                           f"there is no headroom here for a surge or for absorbing "
                           f"volume from elsewhere.")
            elif util <= under_pct:
                verdict = (f"That is at or below the {under_pct:.0f}% threshold while "
                           f"the site carries its full fixed cost.")
            else:
                verdict = (f"That sits between the {under_pct:.0f}% and "
                           f"{over_pct:.0f}% thresholds, so utilisation here is not "
                           f"a finding in either direction.")
            return [KPIInsight(
                theme="Capacity",
                headline=f"{name(only)} is running at {format_pct(util)} of its stated capacity",
                narrative=f"I see {name(only)} running at {format_pct(util)} of its stated "
                          f"capacity. {verdict}",
                metric_refs=refs_for("utilization_pct"),
            )]

        if over:
            named = ", ".join(name(f) for f in over[:3])
            out.append(KPIInsight(
                theme="Capacity",
                headline=f"{len(over)} {_sites(len(over))} are at or above "
                         f"the {over_pct:.0f}% utilisation threshold, with "
                         f"no headroom left",
                severity=InsightSeverity.RISK,
                narrative=(
                    f"I see {named} running at or above {over_pct:.0f}% of stated "
                    f"capacity. At that level there is no headroom left for a "
                    f"demand surge or an outage elsewhere, so this is where "
                    f"service fails first."
                ),
                metric_refs=refs_for("max_utilization_pct"),
            ))
        elif isinstance(max_util, (int, float)) and isinstance(avg_util, (int, float)):
            out.append(KPIInsight(
                theme="Capacity",
                headline=f"No open site reaches the {over_pct:.0f}% "
                         f"threshold, so capacity is not what limits this "
                         f"plan",
                narrative=(
                    f"I see average utilisation at {format_pct(avg_util)} and the "
                    f"busiest site at {format_pct(max_util)}. No open site "
                    f"reaches the "
                    f"{over_pct:.0f}% threshold, so capacity is not what limits "
                    f"this plan."
                ),
                metric_refs=refs_for("avg_utilization_pct"),
                comparison_refs=refs_for("max_utilization_pct"),
            ))

        if len(under) >= 2:
            named = ", ".join(name(f) for f in under[:3])
            out.append(KPIInsight(
                theme="Utilisation",
                headline=f"{len(under)} {_sites(len(under))} run at or below "
                         f"{under_pct:.0f}% utilisation while carrying "
                         f"full fixed cost",
                severity=InsightSeverity.OPPORTUNITY,
                narrative=(
                    f"I see {named} running at or below {under_pct:.0f}% of stated "
                    f"capacity while open and carrying their full fixed cost. "
                    f"Consolidation is worth testing as a scenario; I have not "
                    f"tested it, so I am not stating what it would save."
                ),
                metric_refs=refs_for("avg_utilization_pct"),
            ))
        return out

    @staticmethod
    def _warehouse_insights(warehouse: Dict[str, Any], refs_for,
                            state: Optional[Dict[str, Any]] = None,
                            ) -> List[KPIInsight]:
        """
        What the horizon average was hiding, and where the spend sits.

        Three findings, each emitted only when the evidence carries it:

          * A site whose PEAK period is at or above the threshold while its
            AVERAGE is not. This is the finding a multi-period model exists to
            produce and the one an average cannot state — and it is why this
            method exists beside `_utilization_insights` rather than inside it:
            that one reads the average and is right about the average.
          * How OFTEN the tightest site is tight. "Once in twelve months" and
            "nine months in twelve" are the same peak and different problems.
          * Where the facility spend is concentrated. A total says how much;
            the share says which site to look at.

        Silent when the peak and the average agree everywhere, which is every
        single-period solve — there the existing capacity insight already says
        it, and two cards making one point in different words is worse than
        one.
        """
        from netgravity.config.defaults import UTILIZATION_THRESHOLDS

        if not warehouse:
            return []
        over_pct = UTILIZATION_THRESHOLDS["over_threshold"] * 100.0
        periods = warehouse.get("periods_modelled") or 1
        rows = [r for r in (warehouse.get("tightest") or []) if isinstance(r, dict)]
        out: List[KPIInsight] = []

        def num(row: Dict[str, Any], key: str):
            value = row.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        # ---- the peak the average hid ---------------------------------
        hidden = []
        for row in rows:
            peak, avg = num(row, "peak_utilization_pct"), num(row, "avg_utilization_pct")
            if peak is None or avg is None:
                continue
            if peak >= over_pct > avg:
                hidden.append((row, peak, avg))

        if hidden and periods > 1:
            row, peak, avg = hidden[0]
            name = str(row.get("name") or row.get("facility_id") or "a site")
            when = row.get("peak_period")
            where = f" in period {when}" if when else ""
            others = (f" {len(hidden) - 1} other {_sites(len(hidden) - 1)} in this footprint read the "
                      f"same way." if len(hidden) > 1 else "")
            out.append(KPIInsight(
                theme="Capacity",
                headline=f"{name} is at {format_pct(peak)} in its busiest period, not {format_pct(avg)}",
                severity=InsightSeverity.RISK,
                narrative=(
                    f"I see {name} averaging {format_pct(avg)} of stated capacity across "
                    f"the {periods} modelled periods and reaching "
                    f"{format_pct(peak)}"
                    f"{where}. The average is below the {over_pct:.0f}% threshold "
                    f"and the peak is not, so a reading of the average alone would "
                    f"report headroom this site does not have when it matters."
                    f"{others}"
                ),
                metric_refs=refs_for("peak_utilization_pct"),
                comparison_refs=refs_for("avg_utilization_pct"),
            ))

        # ---- how often ------------------------------------------------
        tight = next((r for r in rows
                      if (num(r, "bottleneck_periods_count") or 0) > 0), None)
        if tight is not None and periods > 1:
            count = int(num(tight, "bottleneck_periods_count") or 0)
            observed = int(num(tight, "periods_observed") or periods)
            name = str(tight.get("name") or tight.get("facility_id") or "a site")
            n_bottlenecks = warehouse.get("n_bottlenecks")
            across = (f" Across the footprint {n_bottlenecks} open {_sites(n_bottlenecks)} reach "
                      f"the threshold at some point in the horizon."
                      if isinstance(n_bottlenecks, int) and n_bottlenecks > 1 else "")
            out.append(KPIInsight(
                theme="Capacity",
                headline=(f"{name} is at or above {over_pct:.0f}% in "
                          f"{count} of {observed} periods"),
                severity=(InsightSeverity.RISK if count > 1
                          else InsightSeverity.INFORMATION),
                narrative=(
                    f"I see {name} at or above {over_pct:.0f}% of stated capacity "
                    f"in {count} of the {observed} modelled periods. That is how "
                    f"often it has no room left, which is a different question "
                    f"from how high it goes.{across}"
                ),
                metric_refs=refs_for("bottleneck_periods_count"),
            ))

        # ---- where the facility spend sits ----------------------------
        #
        # Only when it is CONCENTRATED. A briefing holds six insights; three
        # warehouse cards pushed the footprint and carbon findings off the end
        # of every one, including on networks where the largest site holds a
        # perfectly ordinary share. One site carrying two fifths of what every
        # site costs is worth a reader's attention; one carrying a quarter of
        # four is division.
        drivers = [d for d in (warehouse.get("cost_drivers") or [])
                   if isinstance(d, dict)]
        if drivers:
            top = drivers[0]
            cost = top.get("total_facility_cost")
            share = top.get("share_of_facility_spend")
            if (isinstance(cost, (int, float)) and isinstance(share, (int, float))
                    and share >= _SPEND_CONCENTRATION_SHARE):
                name = str(top.get("name") or top.get("facility_id") or "a site")
                out.append(KPIInsight(
                    theme="Cost",
                    headline=(f"{name} carries {share * 100:.1f}% of what the "
                              f"facilities cost"),
                    narrative=(
                        f"I see {name} accounting for "
                        f"{_money(cost, state or {})} of facility "
                        f"cost, which is {format_pct(share * 100)} of what every site in "
                        f"this plan costs together. That is fixed, opening, "
                        f"handling and holding cost at the site — it does not "
                        f"include transport, so it is where to look first for "
                        f"facility spend and not a ranking of total network cost."
                    ),
                    metric_refs=refs_for("total_facility_cost"),
                ))
        return out

    @staticmethod
    def _cost_structure_insights(state: Dict[str, Any], refs_for) -> List[KPIInsight]:
        """
        Which cost line the total is actually made of.

        A total says how much; the largest component says where to look. Both
        come straight from the solver's own breakdown.
        """
        components = state.get("cost_components") or {}
        priced = {k: v for k, v in components.items()
                  if isinstance(v, (int, float)) and v > 0}
        if len(priced) < 2:
            return []
        largest = max(priced, key=lambda k: priced[k])
        label = largest.replace("_", " ")
        # A cost COMPONENT covers the same span as the total it belongs to. This
        # said "per period" unconditionally, so on a twelve-month horizon it
        # reported a twelve-period total as one period's spend — beside a total
        # in the same list that correctly named its span. The component and the
        # total now describe the same horizon, because they are the same solve.
        #
        # Only the span is reused, not the per-period figure: `cost_per_period`
        # divides the TOTAL, and quoting it here would attach the whole
        # network's monthly cost to one of its lines.
        periods = state.get("periods_modelled")
        span = (" per period" if not isinstance(periods, int) or periods <= 1
                else f" across the {periods} periods modelled")
        return [KPIInsight(
            theme="Cost structure",
            headline=f"{_lead_cap(label)} is the largest single component "
                     f"of what this network costs",
            narrative=(
                f"I see {label} at {_money(priced[largest], state)}{span}, the largest "
                f"single component of this network's cost. Any material saving has "
                f"to come from a line of this size."
            ),
            metric_refs=refs_for(f"cost_components.{largest}"),
        )]

    @staticmethod
    def _footprint_insights(state: Dict[str, Any], refs_for) -> List[KPIInsight]:
        """How many sites the plan holds open, and how many it does not use."""
        opened = state.get("n_facilities_open")
        closed = state.get("n_facilities_closed")
        if not isinstance(opened, (int, float)) or not isinstance(closed, (int, float)):
            return []
        if closed <= 0:
            return []
        return [KPIInsight(
            theme="Footprint",
            headline=f"The plan leaves {closed:.0f} candidate "
                     f"{_sites(closed)} unused",
            # Capacity that is costed and switched off. The decision this
            # finding asks for is whether to bring one IN — not, as the theme
            # alone would suggest, to take one out.
            action_hint="REOPEN_FACILITY",
            severity=InsightSeverity.OPPORTUNITY,
            narrative=(
                f"I see {opened:.0f} {_sites(opened)} open and {closed:.0f} not selected. "
                f"The unselected sites carry no cost in this plan; what they would "
                f"cost and save if opened is a scenario question, and I have not "
                f"run it."
            ),
            metric_refs=refs_for("n_facilities_open"),
            comparison_refs=refs_for("n_facilities_closed"),
        )]

    @staticmethod
    def _carbon_insights(state: Dict[str, Any], refs_for) -> List[KPIInsight]:
        """Emissions, reported only when the network actually computed any."""
        carbon = state.get("total_carbon_kg")
        if not isinstance(carbon, (int, float)) or carbon <= 0:
            return []
        return [KPIInsight(
            theme="Carbon",
            headline="This plan's emissions come from the transport it "
                     "routes, on the declared factors",
            narrative=(
                f"I see {carbon:,.0f} kg of CO2 from the transport in this plan, "
                f"on the declared emission factors. Whether that is priced into "
                f"the objective is a configuration choice, and it does not change "
                f"the quantity."
            ),
            metric_refs=refs_for("total_carbon_kg"),
        )]

    @staticmethod
    def _forecast_insights(forecast: Dict[str, Any], refs_for) -> List[KPIInsight]:
        """
        What the demand projection says, for a reader who has to plan against it.

        Emitted only from figures the forecaster produced. `growth_pct` is None
        — not zero — when no comparable observed window exists, and that case
        produces no growth sentence at all rather than "demand is flat", which
        is a claim nobody made.
        """
        if not forecast:
            return []

        insights: List[KPIInsight] = []
        growth = forecast.get("growth_pct")
        total = forecast.get("total_forecast_units")
        horizon = forecast.get("horizon")

        if isinstance(growth, (int, float)) and isinstance(total, (int, float)):
            direction = "above" if growth >= 0 else "below"
            insights.append(KPIInsight(
                theme="Demand outlook",
                headline=("Demand is projected to grow" if growth >= 0
                          else "Demand is projected to fall"),
                severity=(InsightSeverity.RISK if growth >= 10
                          else InsightSeverity.INFORMATION),
                narrative=(
                    f"I see {total:,.0f} units of demand over the next "
                    f"{horizon} periods, {abs(growth):.1f}% {direction} the same "
                    f"number of periods just observed. That is the volume the "
                    f"current footprint would have to carry."
                ),
                metric_refs=refs_for("forecast.growth_pct"),
            ))
        elif isinstance(total, (int, float)):
            insights.append(KPIInsight(
                theme="Demand outlook",
                headline="A demand projection is available for this network",
                narrative=(
                    f"I see {total:,.0f} units of demand over the next "
                    f"{horizon} periods. There is no comparable observed window "
                    f"to measure growth against, so I do not state a rate."
                ),
                metric_refs=refs_for("forecast.total_forecast_units"),
            ))

        movers = forecast.get("fastest_growing") or []
        named = [m for m in movers
                 if isinstance(m.get("growth_pct"), (int, float))
                 and m["growth_pct"] > 0]
        if named:
            top = named[0]
            insights.append(KPIInsight(
                theme="Where the growth is",
                headline="The growth is not spread evenly across the network",
                narrative=(
                    f"I see the largest increase at {top['market_id']} for "
                    f"{top['product_id']}: {top['forecast_units']:,.0f} units "
                    f"projected against {top['recent_units']:,.0f} observed, "
                    f"{top['growth_pct']:+.1f}%. Growth stated for the whole "
                    f"network loads every site; growth stated where it is "
                    f"happening loads the ones that will actually feel it."
                ),
                metric_refs=refs_for("forecast.fastest_growing"),
            ))

        breaks = forecast.get("n_structural_breaks") or 0
        if breaks:
            insights.append(KPIInsight(
                theme="History that changed",
                headline="Part of this history changed level partway through",
                severity=InsightSeverity.RISK,
                narrative=(
                    f"I see a structural break detected in {breaks} of the "
                    f"series. Where one is found the forecast is built from the "
                    f"period after it rather than from the whole history, "
                    f"because the earlier level is describing a network that no "
                    f"longer exists."
                ),
                metric_refs=refs_for("forecast.structural_breaks"),
            ))

        applied = forecast.get("n_signal_adjustments") or 0
        if applied:
            insights.append(KPIInsight(
                theme="External signals",
                headline="External signals moved part of this forecast",
                narrative=(
                    f"I see {applied} adjustment(s) applied from the market "
                    f"intelligence supplied with this network. These are "
                    f"declared assumptions, not measured effects — the rule "
                    f"that fired is recorded against each one so it can be "
                    f"argued with."
                ),
                metric_refs=refs_for("forecast.signal_adjustments"),
            ))
        return insights

    @staticmethod
    def _forecast_recommendation(forecast: Dict[str, Any]) -> str:
        """
        The next step a forecast actually supports.

        NOT "monitor demand", which is what a briefing says when it has nothing
        to suggest. This application can test the network against the demand
        the forecaster produced, and the growth rate to test at is a figure the
        forecaster already computed — so the recommendation names it, and the
        screen turns it into a scenario.
        """
        growth = forecast.get("growth_pct")
        if not isinstance(growth, (int, float)):
            return ("Read this projection beside the network's own capacity "
                    "before planning against it: the forecast says what is "
                    "coming, not whether the current footprint can carry it.")
        if growth <= 0:
            return ("Consider testing the network at this lower volume: a "
                    "footprint sized for the demand just observed carries fixed "
                    "cost that falling demand does not pay for.")
        movers = [m for m in (forecast.get("fastest_growing") or [])
                  if isinstance(m.get("growth_pct"), (int, float))
                  and m["growth_pct"] > 0]
        where = (f", and scope it to {movers[0]['market_id']} where the increase "
                 f"is concentrated" if movers else "")
        return (f"Consider running a demand scenario at {growth:+.1f}% to see "
                f"whether the current footprint carries this{where}. The "
                f"forecast says what is coming; only a solve says what it costs.")

    @staticmethod
    def _comparison_insights(comparison: Dict[str, Any],
                             alternatives: List[Dict[str, Any]],
                             refs_for,
                             state: Optional[Dict[str, Any]] = None) -> List[KPIInsight]:
        """
        Why the recommended scenario is preferable to the ones beside it.

        "Nagpur costs less" is a fact about Nagpur. "Nagpur costs less than
        expanding Delhi while serving the same demand" is the comparison a
        decision needs, and it is only said when the figures support BOTH
        halves — a cost gap and a demand comparison that is genuinely equal.
        Where demand differs, that is stated instead of glossed, because a
        cheaper plan that serves less is not simply cheaper.
        """
        insights: List[KPIInsight] = []
        winner = comparison.get("recommended_name")
        if not winner or not alternatives:
            return insights

        comparable = [a for a in alternatives
                      if a.get("cost_gap_vs_recommended") is not None]
        for alt in comparable[:2]:
            gap = alt["cost_gap_vs_recommended"]
            fill_gap = alt.get("fill_gap_vs_recommended_pts")

            if gap > 0:
                lead = (f"{winner} costs {_money(gap, state or {})} less "
                        f"than {alt['name']}")
            elif gap < 0:
                lead = (f"{winner} costs {_money(abs(gap), state or {})} "
                        f"MORE than {alt['name']}")
            else:
                lead = f"{winner} and {alt['name']} cost the same"

            # The service half of the trade-off, only where it is measurable.
            if fill_gap is None:
                service = ("Demand served cannot be compared between these two "
                           "solves, so this is a cost comparison only.")
            elif abs(fill_gap) < 0.05:
                service = "Both serve the same demand."
            elif fill_gap > 0:
                service = (f"{alt['name']} serves {fill_gap:,.1f} points more of "
                           f"demand, so the difference is not cost alone.")
            else:
                service = (f"{alt['name']} serves {abs(fill_gap):,.1f} points less "
                           f"of demand.")

            insights.append(KPIInsight(
                theme="Trade-off",
                headline=f"{winner} against {alt['name']}",
                narrative=f"{lead}. {service}",
                severity=(InsightSeverity.OPPORTUNITY if gap > 0
                          else InsightSeverity.INFORMATION),
                metric_refs=refs_for("cost_gap_vs_recommended", limit=2),
            ))

        not_comparable = comparison.get("n_not_comparable") or 0
        if not_comparable:
            insights.append(KPIInsight(
                theme="Not compared",
                headline=f"{not_comparable} of the compared produced no usable cost",
                narrative=(
                    f"{not_comparable} scenario(s) returned no cost the engine "
                    f"could measure against the others, so they are listed but "
                    f"take no position in this ranking."
                ),
                severity=InsightSeverity.RISK,
                metric_refs=refs_for("n_not_comparable"),
            ))
        return insights

    @staticmethod
    def _comparison_recommendation(comparison: Dict[str, Any],
                                   alternatives: List[Dict[str, Any]]) -> str:
        """
        The next step a comparison supports.

        It never says "do this". The ranking is a finding; opening or closing
        a site is classified HUMAN_ONLY by governance whatever the economics
        say, and this sentence must not read as approval.
        """
        winner = comparison.get("recommended_name")
        if not winner:
            return ("I recommend re-running these scenarios: none of them "
                    "produced a cost that can be compared, so there is nothing "
                    "to choose between yet.")
        close = [a for a in alternatives
                 if a.get("cost_gap_vs_recommended") is not None
                 and abs(a["cost_gap_vs_recommended"]) < 1]
        if close:
            return (f"I recommend deciding this on something other than cost: "
                    f"{winner} and {close[0]['name']} are within rounding of each "
                    f"other, so the choice rests on factors this comparison does "
                    f"not measure.")
        return (f"I recommend reviewing {winner} with the people who would have "
                f"to carry it out. The comparison says which is cheaper; whether "
                f"it is the right change is a decision, and not one I make.")

    @staticmethod
    def _recommendation(*, infeasible: bool, state: Dict[str, Any],
                        payload: Dict[str, Any], negatives: List[Dict[str, Any]],
                        insights: List[KPIInsight]) -> str:
        """
        What to do next, chosen by what the evidence actually says.

        Every branch used to collapse to one sentence — "I recommend reviewing
        the quantified impact above before moving to a formal option appraisal"
        — which is not a recommendation. It is the same words whether the
        network strands a fifth of its demand or runs comfortably, so it told a
        reader nothing and, worse, read as considered advice.

        Ordered by what a planner has to deal with first: something that does
        not work, then something at its limit, then something wasteful, then
        nothing. No branch states a saving or an impact figure, because no
        scenario has been run to produce one — naming the next test is a
        recommendation; naming its result would be an invention.
        """
        from netgravity.config.defaults import UTILIZATION_THRESHOLDS

        if infeasible:
            return ("I recommend resolving the constraint conflict with a planner "
                    "before any option appraisal: there is no feasible plan to "
                    "compare options against yet.")

        unserved = state.get("unserved_demand")
        if isinstance(unserved, (int, float)) and unserved > 0:
            return ("I recommend treating the unserved demand first: it is a "
                    "capacity or reachability limit in the plan, and no cost "
                    "comparison is meaningful while part of the demand cannot be "
                    "served at all. Testing added capacity at the constrained "
                    "sites is the scenario I would run next.")

        over_pct = UTILIZATION_THRESHOLDS["over_threshold"] * 100.0
        under_pct = UTILIZATION_THRESHOLDS["under_threshold"] * 100.0
        facilities = [f for f in (payload.get("facilities") or [])
                      if isinstance(f, dict) and f.get("is_open")
                      and isinstance(f.get("utilization_pct"), (int, float))]
        over = [f for f in facilities if f["utilization_pct"] >= over_pct]
        under = [f for f in facilities if f["utilization_pct"] <= under_pct]

        if over:
            return (f"I recommend testing relief for the {len(over)} {_sites(len(over))} at or "
                    f"above the {over_pct:.0f}% utilisation threshold — reassigning "
                    f"volume, or added capacity — because that is where service "
                    f"fails first if demand moves. I have not run that scenario, so "
                    f"I am not stating what it would cost or save.")

        # The peak the average hid. Only sites whose AVERAGE is below the
        # threshold reach here — anything over it on average was returned above
        # — so this branch says something the one above could not.
        warehouse = payload.get("warehouse") or {}
        periods = warehouse.get("periods_modelled") or 1
        peaked = [
            row for row in (warehouse.get("tightest") or [])
            if isinstance(row, dict)
            and isinstance(row.get("peak_utilization_pct"), (int, float))
            and row["peak_utilization_pct"] >= over_pct
        ]
        if peaked and periods > 1:
            row = peaked[0]
            # Capped: this sentence goes into a 350-character field, and
            # exceeding it loses the whole recommendation rather than the tail
            # of it.
            name = str(row.get("name") or row.get("facility_id") or "one site")
            name = name if len(name) <= 32 else name[:31] + "…"
            count = row.get("bottleneck_periods_count")
            observed = row.get("periods_observed") or periods
            when = (f" in {count} of {observed} periods"
                    if isinstance(count, (int, float)) and count else "")
            return (f"I recommend sizing this plan against its peak, not its "
                    f"average: {name} reaches the {over_pct:.0f}% threshold"
                    f"{when} while averaging below it, so it has no room in "
                    f"the period that decides whether the footprint works. "
                    f"Test added capacity or reassigned volume there. I have "
                    f"not run it, so I state no saving.")

        if negatives:
            return ("I recommend a footprint review: at least one open site costs "
                    "more than the routing benefit it provides, so closing it would "
                    "lower cost. Run it as a scenario before acting — closure is "
                    "irreversible and this baseline holds the current footprint "
                    "open by construction.")

        if len(under) >= 2:
            return (f"I recommend testing consolidation of the {len(under)} {_sites(len(under))} "
                    f"at or below {under_pct:.0f}% utilisation. They carry full "
                    f"fixed cost against little volume; whether consolidating them "
                    f"is worth the service cost is exactly what a scenario answers.")

        if not insights:
            return ("I recommend supplying more of the network's data before acting "
                    "on this: I have no deterministic finding to base a "
                    "recommendation on.")

        # Reached only once every reading is clear: demand served, no site over
        # the threshold on average, and — where a horizon was modelled — none
        # over it in any single period either. The sentence names which,
        # because "no site is at its capacity threshold" was previously said on
        # the strength of the average alone.
        basis = ("on average or in any single modelled period" if periods > 1
                 else "in the period modelled")
        return (f"I recommend no structural change on this evidence: demand is "
                f"served, no site reaches its capacity threshold {basis}, and "
                f"nothing is stranded. The next useful step is a scenario "
                f"testing a specific change you are considering, rather than "
                f"one this network is asking for.")

    def _template(
        self,
        payload: Dict[str, Any],
        missing: Optional[Dict[str, Any]] = None,
        scope: ReasoningScope = ReasoningScope.NETWORK,
        entity_id: Optional[str] = None,
        evidence_pack: Optional[ReasoningEvidencePack] = None,
    ) -> ReasoningResult:
        """
        Build a narrative purely from the deterministic figures.

        Not a stub: this is the guaranteed path, and it must stay useful. It
        only ever states values already present in the payload, and names
        anything that is missing rather than passing over it in silence.
        """
        missing = dict(missing or {})
        drivers: List[str] = []
        risks: List[str] = []
        evidence: List[str] = []
        parts: List[str] = []
        insights: List[KPIInsight] = []

        def refs_for(field: str, limit: int = 1) -> List[str]:
            """
            The evidence refs matching one field name.

            `limit` defaults to 1 because a narrative cites one figure per
            clause, and a six-ref citation list behind a one-figure sentence
            would claim a basis the sentence does not use. Callers that render
            a TABLE rather than a sentence pass a higher limit deliberately.
            """
            if evidence_pack is None:
                return []
            return [ref for ref in evidence_pack.metrics
                    if ref == field or ref.endswith(f".{field}")][:limit]

        state = payload.get("network_state") or payload.get("optimization") or {}
        scenario = payload.get("scenario") or {}
        rei_block = payload.get("rei") or {}
        risk_block = payload.get("risk") or {}
        external = payload.get("external_evidence") or {}
        # A LIST, because `OrchestratorRequest` carries one field for market
        # signals whatever route they arrived by and a run may hold several. A
        # single dict is still accepted so a caller assembling a payload by
        # hand — as several tests do — is not silently ignored.
        market_raw = payload.get("market_evidence") or []
        market_signals = [market_raw] if isinstance(market_raw, dict) else list(market_raw)
        # One more deterministic block, read exactly like the others. A
        # COMPARISON-scope pack carries these and no network_state, so the
        # cost/utilisation branches below simply find nothing and say nothing.
        comparison_block = payload.get("comparison") or {}
        comparison_alternatives = payload.get("comparison_alternatives") or []
        # The forecast's own evidence. Absent on every run that is not a
        # forecast, in which case every branch below finds nothing and says
        # nothing — the same contract as the comparison block above.
        forecast_block = payload.get("forecast") or {}
        # The footprint read peak-against-average. Absent on a run that solved
        # nothing, in which case the branch below finds nothing and says
        # nothing — the same contract as the two blocks above.
        warehouse_block = payload.get("warehouse") or {}
        # ONE chart on the KPI screen, asked about by a reader who pressed
        # Explain on it. The chart writes its own deterministic reading beside
        # the numbers it is about (see reasoning/kpi_chart_evidence.py) and
        # this surfaces it; a branch per chart here would put four charts'
        # wording in a file that knows nothing about any of them, and a fifth
        # chart would then need a change in two places.
        #
        # Without this the template writer recognised none of the chart blocks
        # and fell through to "I could not find a deterministic result to
        # explain" — a button that promises a briefing and delivers an apology.
        chart_block = payload.get("kpi_chart") or {}

        infeasible = self._is_infeasible(payload)

        if infeasible:
            parts.append(
                "I found the network INFEASIBLE under this configuration: no valid solution "
                "exists within the current constraints."
            )
            risks.append("No feasible network configuration — constraints conflict.")
            confidence = "LOW"
        else:
            cost = state.get("business_network_cost")
            if cost is not None:
                # What the figure COVERS. It was called "per period"
                # unconditionally, which was true while every solve modelled one
                # period. Over a twelve-month horizon the same sentence
                # overstates the monthly cost twelvefold, in prose a planner is
                # meant to act on — so the span is stated, and the per-period
                # figure quoted beside it comes from the solve rather than from
                # dividing here.
                span = _period_span(state)
                insights.append(KPIInsight(
                    theme="Cost",
                    # A STATEMENT, not "I see ... clearly".
                    #
                    # This headline now leads the recommendation card, and the
                    # card removes the first person from everything a reader
                    # sees — which left "The current cost position clearly", a
                    # fragment, as the first line on the screen. The other
                    # insight headlines survive that removal as sentences; this
                    # one did not, so it is written as one.
                    headline=("This is what the network costs to run today, "
                              "and the baseline every scenario is measured "
                              "against"),
                    narrative=(
                        f"I see business network cost at {_money(cost, state)}"
                        f"{span}. I use this as the decision baseline for "
                        "comparing any scenario."
                    ),
                    metric_refs=refs_for("business_network_cost"),
                ))
                parts.append(
                    f"I see a business network cost of {_money(cost, state)}{span}; "
                    "this is the operating-cost view from the optimizer, separate "
                    "from any mathematical shortage penalty."
                )
                evidence.append(f"business_network_cost = {cost:,.2f}")

            delta = scenario.get("business_cost_delta")
            delta_pct = scenario.get("business_cost_delta_pct")
            if delta is not None:
                direction = "increases" if delta > 0 else "decreases"
                pct = f" ({delta_pct:+.2f}%)" if delta_pct is not None else ""
                parts.append(
                    f"I see the scenario {direction} business cost by "
                    f"{_money(abs(delta), state)}{pct}; this is the incremental "
                    "impact versus the baseline, not the full cost repeated."
                )
                evidence.append(f"business_cost_delta = {delta:,.2f}")
                drivers.append(f"Cost {direction} of {_money(abs(delta), state)} "
                               f"versus baseline")
                # FIRST, on a run that has one.
                #
                # `card_from_briefing` leads with `kpi_insights[0]`, and on a
                # what-if that was the Cost insight above — a sentence about
                # the baseline, ending "the decision baseline for comparing any
                # scenario", printed under the scenario's own name. What the
                # change DID is the finding; what the network costs is the
                # context for it.
                #
                # Ordered here rather than in the card because every consumer
                # of `kpi_insights` had the same problem.
                insights.insert(0, KPIInsight(
                    theme="Scenario impact",
                    # A STATEMENT. This was "I see business cost {direction}
                    # versus baseline", and the card removes the first person
                    # from everything a reader sees — which left "business cost
                    # increases versus baseline", a fragment beginning
                    # lowercase, as the first line on the screen. Exactly the
                    # defect already fixed on the Cost headline above.
                    headline=(f"This change {direction} what the network costs"),
                    narrative=(
                        f"I see an incremental change of "
                        f"{_money(abs(delta), state)}{pct}. This tells me the "
                        "price of the tested network choice before a planner "
                        "weighs the operational benefit."
                    ),
                    metric_refs=refs_for("business_cost_delta"),
                    comparison_refs=refs_for("business_cost_delta_pct"),
                ))

            unserved = state.get("unserved_demand")
            if unserved:
                parts.append(f"I see {unserved:,.0f} units of demand that cannot be served.")
                risks.append(f"Unserved demand of {unserved:,.0f} units.")
                evidence.append(f"unserved_demand = {unserved:,.0f}")

            # Everything below reads figures that were ALREADY in the payload
            # and already narrated in `parts` — service, utilisation, footprint,
            # cost structure, carbon — and turns them into insights.
            #
            # Until now the only themes that produced a KPIInsight were Cost and
            # Scenario impact, so a solved baseline network yielded exactly one
            # insight ("I see the current cost position clearly") no matter what
            # the network said: an overloaded DC, a missed SLA and stranded
            # demand all reached the reader as one cost card, or as nothing at
            # all on any screen that reads `kpi_insights`. The evidence was
            # never the problem; nothing was being made of it.
            #
            # Every insight below is emitted ONLY when its metric is present,
            # and states no figure that is not in the evidence pack — so an
            # absent metric produces an absent insight rather than a confident
            # sentence about a number nobody measured.
            # FIRST on a forecast run, for the same reason the scenario
            # impact leads a what-if: `card_from_briefing` leads with
            # `kpi_insights[0]`, and on a demand projection the reader came
            # for the projection, not for what the network costs today.
            insights.extend(self._forecast_insights(forecast_block, refs_for))
            insights.extend(self._service_insights(state, refs_for))
            insights.extend(self._utilization_insights(state, payload, refs_for))
            # AFTER the average, deliberately. The two answer the same question
            # on different bases, and the peak reading is the correction to the
            # average rather than a replacement for it — so a reader meets the
            # average first and then what it hid.
            insights.extend(self._warehouse_insights(
                warehouse_block, refs_for, state))
            insights.extend(self._cost_structure_insights(state, refs_for))
            insights.extend(self._footprint_insights(state, refs_for))
            insights.extend(self._carbon_insights(state, refs_for))

            confidence = "MEDIUM" if evidence else "LOW"

        top = rei_block.get("highest_exposure_facility") or rei_block.get("top_facility")
        if top:
            rei_val = rei_block.get("max_rei")
            suffix = f" (REI {rei_val:.2f})" if isinstance(rei_val, (int, float)) else ""
            parts.append(f"I see the highest relative economic exposure at {top}{suffix}.")
            drivers.append(f"{top} carries the greatest disruption exposure")
            evidence.append(f"highest_exposure_facility = {top}")
            insights.append(KPIInsight(
                theme="Resilience",
                headline=f"{top} is where losing a single site would cost "
                         f"the most",
                severity=InsightSeverity.RISK,
                narrative=(
                    f"I see {top} carrying the highest relative economic exposure "
                    f"in this network{suffix}. That is where losing one site costs "
                    f"the most, so it is where a contingency is worth the most."
                ),
                metric_refs=refs_for("max_rei"),
                driver_refs=refs_for("highest_exposure_facility"),
            ))

        # A facility whose loss makes the network CHEAPER.
        #
        # It happens, and on this client's network it happens at two sites: the
        # baseline pins their footprint open, so a facility whose fixed cost
        # exceeds its routing benefit shows a negative performance impact when
        # it is removed. The engine has always written a diagnostic saying so,
        # and it has always stopped at the log — leaving a figure on screen
        # that reads as an error in the software rather than a finding about
        # the network. It is stated here, where the figure is reported.
        negatives = [
            row for row in (rei_block.get("facilities") or rei_block.get("results") or [])
            if isinstance(row, dict)
            and isinstance(row.get("performance_impact"), (int, float))
            and row["performance_impact"] < 0
        ]
        if negatives:
            named = ", ".join(str(row.get("facility_id")) for row in negatives[:3])
            parts.append(
                f"I see {len(negatives)} {_sites(len(negatives))} ({named}) whose loss would "
                f"LOWER cost: their fixed cost exceeds the routing benefit they "
                f"provide, so the footprint is worth reviewing."
            )
            insights.append(KPIInsight(
                theme="Footprint",
                headline=f"{len(negatives)} open {_sites(len(negatives))} "
                         f"cost more than the routing they save",
                # The OPPOSITE footprint decision, under the same theme and
                # the same severity as the one above: these sites are open and
                # are not paying for themselves.
                action_hint="CONSOLIDATE",
                severity=InsightSeverity.OPPORTUNITY,
                narrative=(
                    f"I see {len(negatives)} open {_sites(len(negatives))} — {named} — whose "
                    f"removal would REDUCE network cost, because their fixed cost "
                    f"exceeds the routing benefit they provide. The baseline holds "
                    f"the current footprint open, so this is a finding about the "
                    f"footprint rather than an error in the figure."
                ),
            ))
            drivers.append(
                "at least one open facility costs more than the routing benefit it "
                "provides, so its loss reduces network cost rather than raising it")
            evidence.append(
                f"negative_performance_impact = {[row.get('facility_id') for row in negatives]}")

        max_rf = risk_block.get("max_risk_factor")
        if isinstance(max_rf, (int, float)):
            entity = risk_block.get("highest_risk_entity", "the network")
            parts.append(f"I see a combined risk factor of {max_rf:.3f} for {entity}.")
            risks.append(f"Risk factor {max_rf:.3f} at {entity}.")
            evidence.append(f"risk_factor = {max_rf:.3f}")
        elif risk_block.get("not_computable"):
            # RF was attempted and could not be produced. Say why, explicitly,
            # rather than leaving the reader to assume there is no risk.
            reasons = {
                str(row.get("not_computable_reason"))
                for row in risk_block["not_computable"] if isinstance(row, dict)
            }
            parts.append(
                "I see that a combined risk factor was NOT calculated "
                f"({', '.join(sorted(r for r in reasons if r and r != 'None'))}). "
                "Severity and confidence are not probabilities and were not substituted."
            )
            risks.append("Combined risk factor unavailable — see reason above.")

        # Name every missing analysis so absence is never read as a zero.
        if missing:
            described = "; ".join(
                f"{cap} ({info.get('status', 'UNAVAILABLE')})" if isinstance(info, dict)
                else str(cap)
                for cap, info in sorted(missing.items())
            )
            parts.append(
                f"I could not use the following analyses; their values are UNKNOWN "
                f"(not zero): {described}."
            )
            risks.append(f"Incomplete evidence: {described}.")

        if external:
            ev_type = external.get("event_type", "external event")
            loc = external.get("location", "")
            prob = external.get("event_probability")
            severity = external.get("severity") or "UNKNOWN"
            if isinstance(prob, (int, float)):
                like_txt = f", severity {severity}, stated probability {prob:.2f}"
            else:
                # Severity is NOT a probability — say what is known and what is not.
                like_txt = (
                    f", severity {severity}, with NO defensible probability available"
                )
            parts.append(
                f"I see external evidence for {ev_type} affecting {loc}{like_txt} "
                f"(source: {external.get('source', 'unspecified')})."
            )

        if market_signals:
            # No number from this block reaches the summary — not the
            # magnitude, not the guardrail's relevance score, not its
            # threshold, and the title is not quoted either (it is very
            # likely to CONTAIN the magnitude as a substring). None of those
            # are values a deterministic engine computed or verified, and the
            # numeric-claim validator polices every number in generated text
            # regardless of where it came from — quoting the user does not
            # exempt a figure from that check, and it should not: this
            # narrative cannot tell "the user really said this" apart from
            # "the model invented it while claiming the user said it".
            #
            # So this describes the signal only in terms nothing here
            # computed: which category, which direction, whether it cleared
            # the guardrail. The actual figure is not lost — it is on the
            # recorded `MarketIntelligenceSignal` (see the audit trail) — it
            # is simply never asserted as a checked number in prose.
            for market in market_signals:
                verdict = market.get("verdict") or {}
                bucket = market.get("bucket", "UNKNOWN")
                direction = market.get("direction", "NEUTRAL")
                trend = {"UP": "an increase", "DOWN": "a decrease"}.get(
                    direction, "a change")
                if verdict.get("passed"):
                    standing = "cleared the relevance guardrail"
                elif verdict:
                    standing = "did NOT clear the relevance guardrail"
                else:
                    standing = "has not yet been scored against the guardrail"
                parts.append(
                    f"I see a reported market signal: {trend} in the "
                    f"{bucket} category, which {standing}. The reported figure is "
                    f"recorded with the signal, not restated here as a checked "
                    f"number."
                )

        if comparison_block:
            comparison_insights = self._comparison_insights(
                comparison_block, comparison_alternatives, refs_for, state)
            insights.extend(comparison_insights)
            for insight in comparison_insights:
                parts.append(insight.narrative)
            if comparison_block.get("verdict"):
                # The backend's own finding, first. Everything above explains
                # it rather than restating it.
                parts.insert(0, comparison_block["verdict"])
            recommended_cost = comparison_block.get("recommended_cost")
            if recommended_cost is not None:
                evidence.append(f"recommended_cost = {recommended_cost:,.2f}")
            if comparison_block.get("n_not_comparable"):
                risks.append(
                    f"{comparison_block['n_not_comparable']} compared scenario(s) "
                    f"produced no usable cost.")

        if chart_block:
            finding = str(chart_block.get("finding") or "").strip()
            matters = str(chart_block.get("matters") or "").strip()
            # The finding leads, because it is the answer to "what am I
            # looking at"; everything after it explains that rather than
            # restating it.
            if finding:
                parts.insert(0, finding)
            # NOT also into `risks`. The card blanks a meaning that repeats its
            # warning, and `limitation` becomes that warning — so writing this
            # sentence into both slots deleted it from the one a reader looks
            # at first.
            if matters:
                parts.append(matters)

        if not parts:
            parts.append("I could not find a deterministic result to explain for this request.")

        recommendation = (
            self._comparison_recommendation(comparison_block,
                                            comparison_alternatives)
            if comparison_block
            # A forecast's next step is to test the network against the demand
            # it projects, at the rate it projects. The generic recommendation
            # below is about a solved network and has nothing to say about a
            # projection.
            else self._forecast_recommendation(forecast_block)
            if forecast_block
            # A CHART EXPLANATION MAKES NO RECOMMENDATION AT ALL.
            #
            # It says what the chart shows. Telling a reader to close a site
            # or test a scenario needs closure economics, contractual
            # constraints and a second solve — none of which a utilisation
            # chart has — so that belongs to the Overview and the Scenario
            # Planner, which do. Falling through to the network recommendation
            # also printed "I have no deterministic finding to base a
            # recommendation on" directly beneath a stated finding.
            else ""
            if chart_block
            else self._recommendation(
                infeasible=infeasible,
                state=state,
                payload=payload,
                negatives=negatives,
                insights=insights,
            )
        )

        completeness = (
            EvidenceCompleteness.BLOCKED if infeasible else
            EvidenceCompleteness.PARTIAL if missing else
            EvidenceCompleteness.COMPLETE
        )
        # Bounded to what `ReasoningResult.narrative` accepts.
        #
        # It was joined unbounded, and a network with enough to say about it
        # produced a string over the 700-character limit — which failed
        # validation, failed the whole `reasoning.synthesise` capability, and
        # returned an EMPTY summary. Losing every sentence because there was
        # one too many is the worst possible handling of a length limit; the
        # last sentences are dropped and the reader is told.
        summary = _bounded(parts, _MAX_NARRATIVE_CHARS)
        briefing = ExecutiveBriefing(
            scope=scope,
            entity_id=entity_id,
            opening=parts[0],
            context=_bounded(parts[1:], _MAX_NARRATIVE_CHARS),
            # A scoped view answers one question about one thing, so three is
            # the right number there; a network briefing can legitimately have
            # six themes to report.
            kpi_insights=insights[:3 if scope in {ReasoningScope.FACILITY, ReasoningScope.LANE} else 6],
            key_drivers=drivers[:4],
            recommendation=recommendation,
            limitation=(risks[0] if risks else ""),
            missing_information=[
                MissingInformation(
                    question_ref=capability,
                    question=f"Can you provide the missing {capability} evidence?",
                    impact="It would let me strengthen or complete this briefing.",
                    blocking=(completeness is EvidenceCompleteness.BLOCKED),
                )
                for capability in list(sorted(missing))[:2]
            ],
            evidence_completeness=completeness,
            suggested_questions=(
                ["What is driving this result?", "Which node or lane should I examine?"]
                if evidence else []
            ),
        )

        return ReasoningResult(
            summary=summary,
            key_drivers=drivers,
            risks=risks,
            recommendation=recommendation,
            confidence=confidence,
            evidence=evidence,
            briefing=briefing,
            source="template",
        )
