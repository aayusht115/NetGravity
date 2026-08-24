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

from netgravity.orchestrator.agents.llm_gateway import LLMGateway, extract_json
from netgravity.orchestrator.exceptions import LLMFailureError
from netgravity.orchestrator.schemas.risk import ReasoningResult
from netgravity.orchestrator.validation.numeric_grounding import (
    ground_narrative,
    strip_ungrounded_claims,
)

logger = logging.getLogger(__name__)

_VALID_CONFIDENCE = {"LOW", "MEDIUM", "HIGH"}


class ReasoningAgent:
    """Produces narrative synthesis over deterministic evidence."""

    def __init__(self, gateway: Optional[LLMGateway] = None) -> None:
        self.gateway = gateway

    def reason(
        self,
        payload: Dict[str, Any],
        *,
        unavailable_evidence: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, str]] = None,
        allow_llm: bool = True,
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

        if not allow_llm or self.gateway is None or not self.gateway.available:
            result = self._template(payload, missing)
            result.unavailable_evidence = missing
            # The template only ever states values taken from the payload, so
            # it is grounded by construction — but it is checked anyway, because
            # "trust me" is not a validation strategy.
            return self._ground(result, payload, provenance)

        try:
            result = self._llm(payload, missing)
        except LLMFailureError as exc:
            logger.warning("orchestrator.reasoning.llm_failed code=%s", exc.code.value)
            fallback = self._template(payload, missing)
            fallback.unavailable_evidence = missing
            fallback.validation_warnings.append(
                f"LLM reasoning unavailable ({exc.code.value}); deterministic template used."
            )
            return self._ground(fallback, payload, provenance)

        if result is None:
            fallback = self._template(payload, missing)
            fallback.unavailable_evidence = missing
            fallback.validation_warnings.append(
                "LLM reasoning output could not be parsed; deterministic template used."
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
        report = ground_narrative(
            f"{result.summary} {result.recommendation}",
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
        self, payload: Dict[str, Any], missing: Optional[Dict[str, Any]] = None,
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

        state = payload.get("network_state") or payload.get("optimization") or {}
        scenario = payload.get("scenario") or {}
        rei_block = payload.get("rei") or {}
        risk_block = payload.get("risk") or {}
        external = payload.get("external_evidence") or {}
        market = payload.get("market_evidence") or {}

        infeasible = self._is_infeasible(payload)

        if infeasible:
            parts.append(
                "The network is INFEASIBLE under this configuration: no valid solution "
                "exists within the current constraints."
            )
            risks.append("No feasible network configuration — constraints conflict.")
            confidence = "LOW"
        else:
            cost = state.get("business_network_cost")
            if cost is not None:
                parts.append(f"Business network cost is {cost:,.2f} per period.")
                evidence.append(f"business_network_cost = {cost:,.2f}")

            delta = scenario.get("business_cost_delta")
            delta_pct = scenario.get("business_cost_delta_pct")
            if delta is not None:
                direction = "increases" if delta > 0 else "decreases"
                pct = f" ({delta_pct:+.2f}%)" if delta_pct is not None else ""
                parts.append(f"The scenario {direction} business cost by {abs(delta):,.2f}{pct}.")
                evidence.append(f"business_cost_delta = {delta:,.2f}")
                drivers.append(f"Cost {direction} of {abs(delta):,.2f} versus baseline")

            unserved = state.get("unserved_demand")
            if unserved:
                parts.append(f"{unserved:,.0f} units of demand cannot be served.")
                risks.append(f"Unserved demand of {unserved:,.0f} units.")
                evidence.append(f"unserved_demand = {unserved:,.0f}")

            confidence = "MEDIUM" if evidence else "LOW"

        top = rei_block.get("highest_exposure_facility") or rei_block.get("top_facility")
        if top:
            rei_val = rei_block.get("max_rei")
            suffix = f" (REI {rei_val:.2f})" if isinstance(rei_val, (int, float)) else ""
            parts.append(f"Highest relative economic exposure: {top}{suffix}.")
            drivers.append(f"{top} carries the greatest disruption exposure")
            evidence.append(f"highest_exposure_facility = {top}")

        max_rf = risk_block.get("max_risk_factor")
        if isinstance(max_rf, (int, float)):
            entity = risk_block.get("highest_risk_entity", "the network")
            parts.append(f"Combined risk factor for {entity} is {max_rf:.3f}.")
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
                "A combined risk factor was NOT calculated "
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
                f"The following analyses did not complete and their values are UNKNOWN "
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
                f"External evidence: {ev_type} affecting {loc}{like_txt} "
                f"(source: {external.get('source', 'unspecified')})."
            )

        if market:
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
                f"A market signal was reported: {trend} in the "
                f"{bucket} category, which {standing}. The reported figure is "
                f"recorded with the signal, not restated here as a checked "
                f"number."
            )

        if not parts:
            parts.append("No deterministic results were produced for this request.")

        recommendation = (
            "Review the constraint conflict with a planner before proceeding."
            if infeasible else
            "Review the quantified impact above and decide whether to proceed to a "
            "formal option appraisal."
        )

        return ReasoningResult(
            summary=" ".join(parts),
            key_drivers=drivers,
            risks=risks,
            recommendation=recommendation,
            confidence=confidence,
            evidence=evidence,
            source="template",
        )
