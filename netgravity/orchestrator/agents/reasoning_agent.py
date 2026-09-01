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
        return (f" across the {periods} periods modelled "
                f"({per_period:,.2f} per period)")
    return f" across the {periods} periods modelled"


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
        if allow_llm and self.runtime is not None and self.runtime.available:
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
            result = self._llm(payload, missing, user_question)
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

    def _llm(
        self, payload: Dict[str, Any], missing: Dict[str, Any],
        user_question: str = "",
    ) -> Optional[ReasoningResult]:
        assert self.gateway is not None
        # Bound the payload: gateway prompts cap at 100k characters.
        evidence = json.dumps(payload, indent=1, default=str, sort_keys=True)[:40_000]

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
        question = (user_question or "").strip()[:400]
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

        prompt = (
            "DETERMINISTIC RESULTS:\n"
            f"{evidence}\n\n"
            "You are a supply-chain analyst writing for logistics executives. "
            "The figures above are authoritative. Use only those numbers. Do "
            "not verify or recompute them — that is done for you afterwards. "
            "If something is absent, say it is not available rather than "
            "guessing.\n"
            f"{missing_block}"
            f"{ask_block}\n"
            "Write figures the way a person would: thousands separated, "
            "currency to the rupee, percentages to one decimal place.\n"
            "Reply with ONLY this JSON, and keep every string short:\n"
            '{"summary":"<2 sentences, first person (I/my), answering the '
            'question rather than listing figures>",'
            '"recommendation":"<1 sentence, one concrete next step>",'
            '"confidence":"LOW|MEDIUM|HIGH",'
            '"key_drivers":["<6 words>","<6 words>"],'
            '"risks":["<6 words>"],'
            '"evidence":["<one figure quoted from the results>"]}\n'
            "Set confidence to LOW if key results are missing or the network "
            "is infeasible; HIGH only when the results are complete.\n"
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

        return ReasoningResult(
            summary=str(parsed.get("summary", ""))[:2000],
            key_drivers=as_list("key_drivers"),
            risks=as_list("risks"),
            recommendation=str(parsed.get("recommendation", ""))[:1000],
            confidence=confidence,
            evidence=as_list("evidence"),
            source="llm",
            grounded_claims=structured,
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
                headline="I see demand this network cannot serve",
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
            # Stated as the ratio the evidence pack holds, not converted to a
            # percentage: a derived figure would not match the authoritative
            # value the grounding check compares against.
            out.append(KPIInsight(
                theme="Service",
                headline=("I see all stated demand served"
                          if fill >= 1.0 else "I see demand going unmet"),
                narrative=(
                    f"I see a demand fill rate of {fill:.3f}. "
                    + ("Every unit of stated demand is served by this plan, so "
                       "service is not what constrains it."
                       if fill >= 1.0 else
                       "Part of the stated demand is not served by this plan.")
                ),
                metric_refs=refs_for("demand_fill_rate"),
            ))

        if isinstance(sla_pct, (int, float)) and sla_pct < 100.0:
            out.append(KPIInsight(
                theme="Service",
                headline="I see demand served outside its stated lead time",
                severity=InsightSeverity.RISK,
                narrative=(
                    f"I see {sla_pct:.2f}% of demand served within its stated "
                    f"service level. The remainder is served, but not inside the "
                    f"lead time the data commits to."
                ),
                metric_refs=refs_for("pct_demand_in_sla"),
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
                headline=f"I see {name(only)} at {util:.2f}% of stated capacity",
                narrative=f"I see {name(only)} running at {util:.2f}% of its stated "
                          f"capacity. {verdict}",
                metric_refs=refs_for("utilization_pct"),
            )]

        if over:
            named = ", ".join(name(f) for f in over[:3])
            out.append(KPIInsight(
                theme="Capacity",
                headline=f"I see {len(over)} site(s) at or above the "
                         f"{over_pct:.0f}% utilisation threshold",
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
                headline="I see capacity headroom across the footprint",
                narrative=(
                    f"I see average utilisation at {avg_util:.2f}% and the busiest "
                    f"site at {max_util:.2f}%. No open site reaches the "
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
                headline=f"I see {len(under)} site(s) at or below "
                         f"{under_pct:.0f}% utilisation",
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
        return [KPIInsight(
            theme="Cost structure",
            headline=f"I see {label} as the largest cost line",
            narrative=(
                f"I see {label} at {priced[largest]:,.2f} per period, the largest "
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
            headline=f"I see {closed:.0f} candidate site(s) the plan does not use",
            severity=InsightSeverity.OPPORTUNITY,
            narrative=(
                f"I see {opened:.0f} site(s) open and {closed:.0f} not selected. "
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
            headline="I see the transport emissions this plan implies",
            narrative=(
                f"I see {carbon:,.2f} kg of CO2 from the transport in this plan, "
                f"on the declared emission factors. Whether that is priced into "
                f"the objective is a configuration choice, and it does not change "
                f"the quantity."
            ),
            metric_refs=refs_for("total_carbon_kg"),
        )]

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
            return (f"I recommend testing relief for the {len(over)} site(s) at or "
                    f"above the {over_pct:.0f}% utilisation threshold — reassigning "
                    f"volume, or added capacity — because that is where service "
                    f"fails first if demand moves. I have not run that scenario, so "
                    f"I am not stating what it would cost or save.")

        if negatives:
            return ("I recommend a footprint review: at least one open site costs "
                    "more than the routing benefit it provides, so closing it would "
                    "lower cost. Run it as a scenario before acting — closure is "
                    "irreversible and this baseline holds the current footprint "
                    "open by construction.")

        if len(under) >= 2:
            return (f"I recommend testing consolidation of the {len(under)} site(s) "
                    f"at or below {under_pct:.0f}% utilisation. They carry full "
                    f"fixed cost against little volume; whether consolidating them "
                    f"is worth the service cost is exactly what a scenario answers.")

        if not insights:
            return ("I recommend supplying more of the network's data before acting "
                    "on this: I have no deterministic finding to base a "
                    "recommendation on.")

        return ("I recommend no structural change on this evidence: demand is "
                "served, no site is at its capacity threshold, and nothing in the "
                "plan is stranded. The next useful step is a scenario that tests a "
                "specific change you are considering, rather than a change this "
                "network is asking for.")

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
                    headline="I see the current cost position clearly",
                    narrative=(
                        f"I see business network cost at {cost:,.2f}{span}. I use "
                        "this as the decision baseline for comparing any scenario."
                    ),
                    metric_refs=refs_for("business_network_cost"),
                ))
                parts.append(
                    f"I see a business network cost of {cost:,.2f}{span}; this is "
                    "the operating-cost view from the optimizer, separate from any "
                    "mathematical shortage penalty."
                )
                evidence.append(f"business_network_cost = {cost:,.2f}")

            delta = scenario.get("business_cost_delta")
            delta_pct = scenario.get("business_cost_delta_pct")
            if delta is not None:
                direction = "increases" if delta > 0 else "decreases"
                pct = f" ({delta_pct:+.2f}%)" if delta_pct is not None else ""
                parts.append(
                    f"I see the scenario {direction} business cost by "
                    f"{abs(delta):,.2f}{pct}; this is the incremental impact versus "
                    "the baseline, not the full cost repeated."
                )
                evidence.append(f"business_cost_delta = {delta:,.2f}")
                drivers.append(f"Cost {direction} of {abs(delta):,.2f} versus baseline")
                insights.append(KPIInsight(
                    theme="Scenario impact",
                    headline=f"I see business cost {direction} versus baseline",
                    narrative=(
                        f"I see an incremental change of {abs(delta):,.2f}{pct}. "
                        "This tells me the price of the tested network choice before "
                        "a planner weighs the operational benefit."
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
            insights.extend(self._service_insights(state, refs_for))
            insights.extend(self._utilization_insights(state, payload, refs_for))
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
                headline=f"I see the greatest single-site exposure at {top}",
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
                f"I see {len(negatives)} facility(ies) ({named}) whose loss would "
                f"LOWER cost: their fixed cost exceeds the routing benefit they "
                f"provide, so the footprint is worth reviewing."
            )
            insights.append(KPIInsight(
                theme="Footprint",
                headline="I see sites that cost more than the routing they save",
                severity=InsightSeverity.OPPORTUNITY,
                narrative=(
                    f"I see {len(negatives)} open facility(ies) — {named} — whose "
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

        if not parts:
            parts.append("I could not find a deterministic result to explain for this request.")

        recommendation = self._recommendation(
            infeasible=infeasible,
            state=state,
            payload=payload,
            negatives=negatives,
            insights=insights,
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
