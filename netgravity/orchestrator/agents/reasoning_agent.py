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
from netgravity.orchestrator.reasoning.evidence import build_evidence_pack
from netgravity.orchestrator.reasoning.runtime import ReasoningRuntime
from netgravity.orchestrator.reasoning.validation import validate_reasoning_draft
from netgravity.orchestrator.schemas.reasoning import (
    EvidenceCompleteness,
    ExecutiveBriefing,
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
            result = self._llm(payload, missing)
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
            logger.warning(
                "orchestrator.reasoning.grounding_failed source=%s contradicted=%d "
                "unsupported=%d",
                result.source, len(report.contradicted), len(report.unsupported),
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

        prompt = (
            "You are a supply-chain network analyst writing for logistics executives.\n"
            "You are given DETERMINISTIC results from a MILP optimizer, KPI engine, "
            "resilience (REI) engine and risk calculator.\n\n"
            "CRITICAL RULES:\n"
            "- These numbers are authoritative. Never recompute, adjust, estimate or "
            "contradict them.\n"
            "- Never invent a figure that is not present in the data below. Every number "
            "you write is checked against the authoritative values, and any figure that "
            "does not match will be REMOVED from your answer.\n"
            "- If something is absent or null, say it is not available. Do not guess.\n"
            "- Do not claim feasibility, cost or service outcomes beyond what is stated.\n\n"
            "- Write the summary and recommendation in first-person singular (I/my), "
            "never we/our. Lead with the decision-relevant finding and explain what "
            "the KPIs mean instead of dumping figures.\n\n"
            # Length matters here, not just shape. The gateway caps OUTPUT at
            # MAX_OUTPUT_TOKENS and gpt-5-mini spends part of that budget on
            # internal reasoning, so a verbose answer is truncated mid-JSON and
            # arrives unparseable. The caps below match what the parser keeps
            # anyway (8 list items, 400 chars each), so asking for less discards
            # nothing that would have survived.
            "Be concise. Keep the whole reply under 250 words: at most 3 "
            "key_drivers, 2 risks, 4 evidence entries and 6 claims, each a "
            "single short sentence. A reply that runs long is truncated and "
            "discarded entirely.\n"
            "Return ONLY a JSON object, no prose and no code fences:\n"
            "{\n"
            '  "summary": "2-4 sentences on what the analysis shows",\n'
            '  "key_drivers": ["driver 1", "driver 2"],\n'
            '  "risks": ["risk 1"],\n'
            '  "recommendation": "one concrete recommended next step",\n'
            '  "confidence": "LOW" | "MEDIUM" | "HIGH",\n'
            '  "evidence": ["specific figure cited from the data"],\n'
            '  "claims": [\n'
            '    {"type": "field name from the data", "value": <number>, '
            '"unit": "percent|currency|units|ratio", "text": "as written in your summary"}\n'
            "  ]\n"
            "}\n\n"
            "The \"claims\" array must list EVERY number you used, with the value exactly "
            "as it appears in the deterministic data. This is how your figures are "
            "verified.\n"
            "Set confidence to LOW if key results are missing or the network is "
            "infeasible; HIGH only when results are complete and unambiguous.\n"
            f"{missing_block}\n"
            f"DETERMINISTIC RESULTS:\n{evidence}\n"
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

        def refs_for(field: str) -> List[str]:
            if evidence_pack is None:
                return []
            return [ref for ref in evidence_pack.metrics
                    if ref == field or ref.endswith(f".{field}")][:1]

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
                parts.append(
                    f"I see a business network cost of {cost:,.2f} per period; this is "
                    "the operating-cost view from the optimizer, separate from any "
                    "mathematical shortage penalty."
                )
                evidence.append(f"business_network_cost = {cost:,.2f}")
                insights.append(KPIInsight(
                    theme="Cost",
                    headline="I see the current cost position clearly",
                    narrative=(
                        f"I see business network cost at {cost:,.2f} per period. I use "
                        "this as the decision baseline for comparing any scenario."
                    ),
                    metric_refs=refs_for("business_network_cost"),
                ))

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

            confidence = "MEDIUM" if evidence else "LOW"

        top = rei_block.get("highest_exposure_facility") or rei_block.get("top_facility")
        if top:
            rei_val = rei_block.get("max_rei")
            suffix = f" (REI {rei_val:.2f})" if isinstance(rei_val, (int, float)) else ""
            parts.append(f"I see the highest relative economic exposure at {top}{suffix}.")
            drivers.append(f"{top} carries the greatest disruption exposure")
            evidence.append(f"highest_exposure_facility = {top}")

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

        recommendation = (
            "I recommend reviewing the constraint conflict with a planner before proceeding."
            if infeasible else
            "I recommend reviewing the quantified impact above before moving to a "
            "formal option appraisal."
        )

        completeness = (
            EvidenceCompleteness.BLOCKED if infeasible else
            EvidenceCompleteness.PARTIAL if missing else
            EvidenceCompleteness.COMPLETE
        )
        summary = " ".join(parts)
        briefing = ExecutiveBriefing(
            scope=scope,
            entity_id=entity_id,
            opening=parts[0],
            context=" ".join(parts[1:]),
            kpi_insights=insights[:3 if scope in {ReasoningScope.FACILITY, ReasoningScope.LANE} else 4],
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
